"""
Inventory forecasting service — parallel pipeline for stock forecasts using ML models.
Reuses shared training/inference utilities from forecasting_service.py.
Targets STOCK_START_OF_PERIOD aggregated by PRODUCT_FAMILY + global total.
"""

# ============================================================================
# EXTENSION GUIDE: New 5G Stock Forecasting Parameters
# ============================================================================
# This service is ready to support three new features for advanced inventory
# forecasting on the 5G dataset:
#
# 1. FORECAST_TARGET (stock vs demand):
#    Current: Only targets stock (current_stock_qty)
#    Extension: Add `forecast_target` parameter to switch between:
#      - "stock": Current implementation (QTE_STK / current_stock_qty)
#      - "demand": New target = QTE_VTE + ACTIVATIONS (sales + activations)
#    
#    Implementation:
#    - Modify load_inventory_history() to accept forecast_target parameter
#    - When forecast_target="demand":
#      - Query: SUM(sales_qty + activations_qty) instead of current_stock_qty
#      - Use this combined metric as the forecast target variable
#    - Reuse all existing feature engineering and model training logic
#
# 2. FORECAST_SCOPE (national, by_product_type, by_governorate):
#    Current: Only aggregates by product_family (and global)
#    Extension: Add `forecast_scope` parameter for:
#      - "national": Current (all governorates + families aggregated)
#      - "by_product_type": Group by PRODUCT_TYPE (SUBSCRIPTION, CPE, SMARTPHONE)
#      - "by_governorate": Group by GOVERNORATE + PRODUCT_TYPE (per-site forecasts)
#    
#    Implementation:
#    - Modify _aggregate_global_and_families() to accept scope parameter
#    - When scope="by_governorate":
#      - Aggregate by (governorate, product_type, date) instead of product_family
#      - Apply minimum data threshold: only forecast if ≥6 non-zero months available
#      - Optimize by skipping sparse combinations
#    - Store governorate in results for per-site recommendations
#
# 3. ACTIVATIONS_QTY as Exogenous Regressor:
#    Current: Features are historical lags/rolling stats only
#    Extension: Add `activations_qty` as exogenous input for Prophet/XGBoost:
#    
#    Implementation:
#    - In _add_inventory_features() or new _add_exogenous_features():
#      - Query activations_qty from mart.fact_stock
#      - Align by (date, product_family, governorate)
#      - Include in Prophet forecast as regressor
#      - Include in XGBoost as additional feature column
#    - For non-supported models (naive, seasonal): ignore this feature
#    - Weight exogenous feature heavily for SUBSCRIPTION/CPE products
#
# 4. DATA_SOURCE Weighting:
#    Current: No distinction between REAL and SIMULATED data
#    Extension: Apply sample weights in training:
#    
#    Implementation:
#    - In train_inventory_models():
#      - Query data_source for each historical row
#      - Create sample_weight: 1.0 for REAL, 0.5 for SIMULATED
#      - Pass sample_weight to Prophet (via uncertainty_samples increase)
#      - Pass sample_weight to XGBoost (native support)
#      - Propagate to sklearn models via fit(weights=...)
#    - Increase uncertainty_samples in Prophet linearly with simulated % share
#
# Integration Notes:
# - Use SQLAlchemy Session pattern (already in place)
# - Reuse _fill_temporal_gaps, _preprocess_inventory_data, _run_forecast_for_model
# - All changes backward-compatible (parameters optional, default to current behavior)
# - Add integration tests for each new parameter combination
# - Update inventory API (InventoryForecastRequest) to accept new parameters
#
# ============================================================================
from __future__ import annotations

import contextvars
import json
import math
import logging
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any, List, Dict, Tuple, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session
from sklearn.model_selection import TimeSeriesSplit

from opentelemetry import trace
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

from app.core.config import settings
from app.core.tracing import get_tracer
from app.services.forecasting_service import (
    _normalize_model_key,
    _fill_temporal_gaps,
    _build_features,
    _metrics,
    _run_forecast_for_model,
    get_available_generative_models,
    CLASSIC_MODELS,
    GENERATIVE_MODELS,
    MONTHLY_FREQ,
    DAILY_FREQ,
    MONTHLY_SEASONAL_PERIOD,
    DAILY_SEASONAL_PERIOD,
)

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value


# Weighted scoring for multi-metric model selection
# Updated: WAPE replaces MAPE for better inventory handling (no zero division issues)
# Weights: WAPE 0.40, RMSE 0.25, MAE 0.20, SMAPE 0.15
METRIC_WEIGHTS = {
    "wape": 0.40,
    "rmse": 0.25,
    "mae": 0.20,
    "smape": 0.15,
}

FORECAST_TARGET_STOCK = "stock"
FORECAST_TARGET_DEMAND = "demand"
FORECAST_SCOPE_NATIONAL = "national"
FORECAST_SCOPE_BY_PRODUCT_TYPE = "by_product_type"
FORECAST_SCOPE_BY_GOVERNORATE = "by_governorate"


# Classic (always-available) inventory models. Generative models are appended at
# runtime from get_available_generative_models() depending on what's installed.
INVENTORY_BASE_MODELS = [
    "naive_last",
    "seasonal_naive",
    "prophet",
    "lstm",
]

# Minimum history points each model needs before it will fit. A family series
# shorter than this (e.g. a newly-added product with 2 months of data) would make
# these models raise "Insufficient history", dropping the family from the aggregate.
# Instead we downgrade to a model that tolerates short series (see _pick_effective_model).
MODEL_MIN_HISTORY_POINTS: Dict[str, int] = {
    "lstm": 8,
    "patchtst": 8,
    "autogluon": 8,
    "prophet": 8,
    "sarima": 8,
}


def _pick_effective_model(model_key: str, n_points: int, seasonal_period: int) -> str:
    """Downgrade the chosen model when the series is too short to fit it.

    Falls back to seasonal_naive when there's at least a full season of history,
    otherwise naive_last (which only needs one point). Returns model_key unchanged
    when the series is long enough.
    """
    min_needed = MODEL_MIN_HISTORY_POINTS.get(model_key, 1)
    if n_points >= min_needed:
        return model_key
    return "seasonal_naive" if n_points >= seasonal_period else "naive_last"


def resolve_inventory_models(
    selected_models: list[str] | None,
    enable_generative: bool,
) -> list[str]:
    """Return the exact list of models that train_inventory_models will run.

    Mirrors the selection logic inside train_inventory_models so callers (e.g. the
    training endpoint) can size progress bars accurately: only generative models
    that are actually installed are included, and a non-'all' selection is honored.
    """
    selected_lower = [_normalize_model_key(m) for m in (selected_models or [])]
    run_all = not selected_lower or "all" in selected_lower

    models = INVENTORY_BASE_MODELS.copy()
    if enable_generative:
        models.extend(get_available_generative_models())

    if not run_all:
        models = [m for m in models if m in selected_lower]
    return models


def _normalize_forecast_target(forecast_target: str | None) -> str:
    """Normalize the selected inventory target to a supported canonical value."""
    normalized = (forecast_target or FORECAST_TARGET_STOCK).strip().lower()
    if normalized not in {FORECAST_TARGET_STOCK, FORECAST_TARGET_DEMAND}:
        raise ValueError(f"Unsupported forecast_target: {forecast_target}")
    return normalized


def _normalize_forecast_scope(forecast_scope: str | None) -> str:
    """Normalize the selected scope to a supported canonical value."""
    normalized = (forecast_scope or FORECAST_SCOPE_NATIONAL).strip().lower()
    if normalized not in {
        FORECAST_SCOPE_NATIONAL,
        FORECAST_SCOPE_BY_PRODUCT_TYPE,
        FORECAST_SCOPE_BY_GOVERNORATE,
    }:
        raise ValueError(f"Unsupported forecast_scope: {forecast_scope}")
    return normalized


def _inventory_scope_expression(forecast_scope: str) -> str:
    """Return the SQL expression used to bucket inventory history by scope."""
    if forecast_scope == FORECAST_SCOPE_BY_PRODUCT_TYPE:
        return "COALESCE(NULLIF(TRIM(dp.type_prod), ''), COALESCE(dp.product_family, 'UNKNOWN'))"
    if forecast_scope == FORECAST_SCOPE_BY_GOVERNORATE:
        return (
            "COALESCE(NULLIF(TRIM(dg.governorate), ''), 'UNKNOWN') || ' | ' || "
            "COALESCE(NULLIF(TRIM(dp.type_prod), ''), COALESCE(dp.product_family, 'UNKNOWN'))"
        )
    return "COALESCE(dp.product_family, 'UNKNOWN')"


def _inventory_target_expression(forecast_target: str) -> str:
    """Return the SQL expression used to compute the active forecast target."""
    if forecast_target == FORECAST_TARGET_DEMAND:
        return "COALESCE(fs.sales_qty, 0) + COALESCE(fs.activations_qty, 0)"
    return "COALESCE(fs.current_stock_qty, fs.stock_quantity, 0)"


def _trim_trailing_zero_periods(
    series: pd.DataFrame,
    value_col: str = "stock_value",
    min_keep: int = 6,
) -> pd.DataFrame:
    """Drop trailing all-zero periods from a date-sorted series.

    The demand target (sales+activations) is 0 for future-dated stock snapshots that
    haven't accrued demand yet (e.g. months after the current date). Left in place,
    walk-forward CV evaluates models on these empty tail months and WAPE explodes
    (prediction ~history level vs actual ~0). Trimming them makes CV score real demand
    only. Stock target is unaffected — its tail is non-zero, so nothing is trimmed.

    Keeps at least `min_keep` rows so a series that is mostly zeros still trains.
    """
    if series.empty or value_col not in series.columns:
        return series
    vals = pd.to_numeric(series[value_col], errors="coerce").fillna(0.0).to_numpy()
    last_nonzero = -1
    for i in range(len(vals) - 1, -1, -1):
        if abs(vals[i]) > 1e-9:
            last_nonzero = i
            break
    if last_nonzero < 0:
        return series  # entirely zero — nothing meaningful to trim to
    keep = max(last_nonzero + 1, min(min_keep, len(series)))
    if keep >= len(series):
        return series
    trimmed = series.iloc[:keep]
    logger.info(
        "Trimmed %d trailing zero-demand period(s) before CV (kept %d of %d)",
        len(series) - keep, keep, len(series),
    )
    return trimmed


def _mean_percentage_error(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Compute Mean Percentage Error (MPE) — can be negative."""
    safe = np.where(y_true == 0, 1, y_true)
    return float(np.mean((y_pred - safe) / safe) * 100.0)


def _wape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """
    Compute Weighted Absolute Percentage Error (WAPE).
    Better than MAPE for inventory because it handles zeros robustly.
    Formula: sum(|y_true - y_pred|) / sum(|y_true|) * 100
    """
    numerator = np.sum(np.abs(y_true - y_pred))
    denominator = np.sum(np.abs(y_true))
    if denominator == 0:
        return 0.0
    return float((numerator / denominator) * 100.0)


def _weighted_model_score(
    mae: float,
    rmse: float,
    wape: float,
    smape: float,
) -> float:
    """
    Compute weighted score across metrics.
    Lower is better. Normalized by dividing each metric by a baseline.
    Uses WAPE instead of MAPE for better handling of inventory data with zeros.
    """
    # Normalize metrics to 0-100 scale (assume max realistic values)
    # These are soft ceilings; actual values may exceed them
    wape_norm = min(100.0, wape) / 100.0
    rmse_norm = min(100.0, rmse) / 100.0
    mae_norm = min(100.0, mae) / 100.0
    smape_norm = min(100.0, smape) / 100.0

    score = (
        METRIC_WEIGHTS["wape"] * wape_norm
        + METRIC_WEIGHTS["rmse"] * rmse_norm
        + METRIC_WEIGHTS["mae"] * mae_norm
        + METRIC_WEIGHTS["smape"] * smape_norm
    )
    return score


def load_inventory_history(
    db: Session,
    granularity: str = "monthly",
    forecast_target: str = FORECAST_TARGET_STOCK,
    forecast_scope: str = FORECAST_SCOPE_NATIONAL,
    service_type: str | None = None,
) -> pd.DataFrame:
    """
    Load aggregated inventory history using the selected target and scope.

    forecast_target:
    - stock: current_stock_qty/current stock on hand
    - demand: QTE_VTE + ACTIVATIONS (sales + activations demand proxy)

    forecast_scope:
    - national: aggregate by product_family as before
    - by_product_type: aggregate by product type
    - by_governorate: aggregate by governorate + product type

    service_type: optional filter — FIBRE | 5G | DATA_BUNDLE | VOD
    """
    forecast_target = _normalize_forecast_target(forecast_target)
    forecast_scope = _normalize_forecast_scope(forecast_scope)
    scope_expr = _inventory_scope_expression(forecast_scope)
    target_expr = _inventory_target_expression(forecast_target)

    # When a service_type filter is provided we join dim_services and add a WHERE clause.
    service_join = "INNER JOIN mart.dim_services ds ON dp.service_id = ds.service_id" if service_type else ""
    service_filter = "AND ds.service_code = :service_type" if service_type else ""
    params: dict = {"service_type": service_type} if service_type else {}

    if granularity == "monthly":
        query = """
            WITH last_snap AS (
                SELECT
                    DISTINCT ON ({scope_expr}, DATE_TRUNC('month', dt.date)::date, fs.product_id)
                    DATE_TRUNC('month', dt.date)::date AS month,
                    {scope_expr} AS product_family,
                    COALESCE(NULLIF(TRIM(dp.type_prod), ''), COALESCE(dp.product_family, 'UNKNOWN')) AS product_type,
                    COALESCE(NULLIF(TRIM(dg.governorate), ''), 'NATIONAL') AS governorate,
                    fs.product_id,
                    {target_expr}::numeric AS stock_value,
                    COALESCE(fs.data_source, 'REAL') AS data_source,
                    dt.date AS dt_date,
                    fs.snapshot_date
                FROM mart.fact_stock fs
                INNER JOIN mart.dim_temps dt ON fs.date_id = dt.date_id
                INNER JOIN mart.dim_products dp ON fs.product_id = dp.product_id
                {service_join}
                LEFT JOIN mart.dim_geographie dg ON fs.geo_id = dg.geo_id
                WHERE {target_expr} IS NOT NULL {service_filter}
                ORDER BY {scope_expr}, DATE_TRUNC('month', dt.date)::date, fs.product_id, dt.date DESC, fs.snapshot_date DESC
            )
            SELECT
                month AS date,
                product_family,
                product_type,
                governorate,
                SUM(stock_value)::numeric AS stock_value,
                COUNT(*) AS record_count,
                SUM(CASE WHEN data_source = 'REAL' THEN 1 ELSE 0 END)::numeric AS real_count,
                SUM(CASE WHEN data_source != 'REAL' THEN 1 ELSE 0 END)::numeric AS simulated_count
            FROM last_snap
            GROUP BY month, product_family, product_type, governorate
            ORDER BY date ASC
        """.format(scope_expr=scope_expr, target_expr=target_expr,
                   service_join=service_join, service_filter=service_filter)
    else:
        # Daily: aggregate by calendar date
        query = """
            SELECT
                dt.date::date AS date,
                {scope_expr} AS product_family,
                COALESCE(NULLIF(TRIM(dp.type_prod), ''), COALESCE(dp.product_family, 'UNKNOWN')) AS product_type,
                COALESCE(NULLIF(TRIM(dg.governorate), ''), 'NATIONAL') AS governorate,
                SUM({target_expr})::numeric AS stock_value,
                COUNT(*) AS record_count,
                SUM(CASE WHEN COALESCE(fs.data_source, 'REAL') = 'REAL' THEN 1 ELSE 0 END)::numeric AS real_count,
                SUM(CASE WHEN COALESCE(fs.data_source, 'REAL') = 'REAL' THEN 0 ELSE 1 END)::numeric AS simulated_count
            FROM mart.fact_stock fs
            INNER JOIN mart.dim_temps dt ON fs.date_id = dt.date_id
            INNER JOIN mart.dim_products dp ON fs.product_id = dp.product_id
            {service_join}
            LEFT JOIN mart.dim_geographie dg ON fs.geo_id = dg.geo_id
            WHERE {target_expr} IS NOT NULL {service_filter}
            GROUP BY
                dt.date::date,
                {scope_expr},
                COALESCE(NULLIF(TRIM(dp.type_prod), ''), COALESCE(dp.product_family, 'UNKNOWN')),
                COALESCE(NULLIF(TRIM(dg.governorate), ''), 'NATIONAL')
            ORDER BY date ASC
        """.format(scope_expr=scope_expr, target_expr=target_expr,
                   service_join=service_join, service_filter=service_filter)

    rows = db.execute(text(query), params).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "product_family",
                "product_type",
                "governorate",
                "stock_value",
                "record_count",
                "real_count",
                "simulated_count",
            ]
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "date",
            "product_family",
            "product_type",
            "governorate",
            "stock_value",
            "record_count",
            "real_count",
            "simulated_count",
        ],
    )
    df["date"] = pd.to_datetime(df["date"])
    # Ensure numeric columns are float to prevent Decimal type issues
    df["stock_value"] = pd.to_numeric(df["stock_value"], errors="coerce").astype(float)
    df["record_count"] = pd.to_numeric(df["record_count"], errors="coerce").astype(float)
    df["real_count"] = pd.to_numeric(df["real_count"], errors="coerce").fillna(0.0)
    df["simulated_count"] = pd.to_numeric(df["simulated_count"], errors="coerce").fillna(0.0)
    return df.sort_values("date")


def _add_inventory_features(
    df: pd.DataFrame,
    granularity: str,
) -> pd.DataFrame:
    """
    Add exogenous features for inventory forecasting.
    Creates lag features, rolling statistics, and temporal features.

    Monthly features:
    - stock_lag_1/2/3, stock_lag_seasonal (12-month annual cycle)
    - stock_roll_3/6/12, stock_roll_std_3, stock_volatility_3m
    - month, quarter, month_sin, month_cos, trend_index, stock_momentum, stock_acceleration

    Daily features:
    - stock_lag_1/2/3, stock_lag_seasonal (7-day weekly cycle)
    - stock_roll_3/6/12, stock_roll_std_3, stock_volatility_3m
    - month, quarter, dayofweek, dayofyear,
      dayofweek_sin/cos (period 7), dayofyear_sin/cos (period 365.25),
      trend_index, stock_momentum, stock_acceleration
    """
    df = df.copy()
    df = df.sort_values("date").reset_index(drop=True)

    # One full seasonal cycle: 12 months for monthly data, 7 days (weekly) for daily data
    seasonal_lag = MONTHLY_SEASONAL_PERIOD if granularity == "monthly" else DAILY_SEASONAL_PERIOD

    # Lag features — consistent column name regardless of granularity
    df["stock_lag_1"] = df["stock_value"].shift(1)
    df["stock_lag_2"] = df["stock_value"].shift(2)
    df["stock_lag_3"] = df["stock_value"].shift(3)
    df["stock_lag_seasonal"] = df["stock_value"].shift(seasonal_lag)

    # Rolling statistics (detect trends and volatility)
    df["stock_roll_3"] = df["stock_value"].rolling(window=3, min_periods=1).mean()
    df["stock_roll_6"] = df["stock_value"].rolling(window=6, min_periods=1).mean()
    df["stock_roll_12"] = df["stock_value"].rolling(window=12, min_periods=1).mean()
    df["stock_roll_std_3"] = df["stock_value"].rolling(window=3, min_periods=1).std().fillna(0)
    df["stock_volatility_3m"] = (df["stock_roll_std_3"] / (df["stock_roll_3"] + 1e-6)).clip(upper=1.0)

    # Trend index
    df["trend_index"] = np.arange(len(df))

    # Month and quarter are useful at both granularities as a coarse seasonal signal
    df["month"] = df["date"].dt.month
    df["quarter"] = df["date"].dt.quarter

    if granularity == "monthly":
        # Annual cyclical encoding (period = 12 months)
        df["month_sin"] = np.sin(2 * np.pi * df["month"] / 12)
        df["month_cos"] = np.cos(2 * np.pi * df["month"] / 12)
    else:
        # Weekly cyclical encoding (period = 7 days) captures Mon-Sun pattern
        df["dayofweek"] = df["date"].dt.dayofweek
        df["dayofweek_sin"] = np.sin(2 * np.pi * df["dayofweek"] / 7)
        df["dayofweek_cos"] = np.cos(2 * np.pi * df["dayofweek"] / 7)
        # Annual cyclical encoding using day-of-year (period ≈ 365.25)
        df["dayofyear"] = df["date"].dt.dayofyear
        df["dayofyear_sin"] = np.sin(2 * np.pi * df["dayofyear"] / 365.25)
        df["dayofyear_cos"] = np.cos(2 * np.pi * df["dayofyear"] / 365.25)

    # Stock health indicators
    df["stock_momentum"] = df["stock_value"] - df["stock_roll_3"]
    df["stock_acceleration"] = df["stock_momentum"].diff().fillna(0)

    # Fill remaining NaNs from lag operations (numeric only — fillna(0.0) on datetime converts NaT to epoch)
    numeric_cols = df.select_dtypes(include="number").columns
    df[numeric_cols] = df[numeric_cols].ffill().fillna(0.0)

    return df


def _preprocess_inventory_data(
    df: pd.DataFrame,
    granularity: str,
) -> pd.DataFrame:
    """
    Preprocess inventory data with robust handling of:
    1. Missing values (forward-fill + linear interpolation)
    2. Outlier detection/smoothing using IQR
    3. Family normalization to prevent under-weighting smaller families
    
    Returns preprocessed DataFrame with cleaned and normalized stock values.
    """
    df = df.copy().sort_values(["product_family", "date"]).reset_index(drop=True)
    
    # ========== Step 1: Handle missing values per family ==========
    logger.info("📋 Preprocessing: Handling missing values...")
    for family in df["product_family"].unique():
        mask = df["product_family"] == family
        
        # Forward-fill first (carry last known value forward)
        df.loc[mask, "stock_value"] = df.loc[mask, "stock_value"].ffill()

        # Then backward-fill for any remaining leading NaNs
        df.loc[mask, "stock_value"] = df.loc[mask, "stock_value"].bfill()
        
        # Finally, linear interpolation for any internal gaps
        df.loc[mask, "stock_value"] = df.loc[mask, "stock_value"].interpolate(
            method="linear", limit_direction="both"
        )
    
    # Replace any remaining NaNs with 0 (should be rare after above steps)
    df["stock_value"] = df["stock_value"].fillna(0.0)
    
    # ========== Step 2: Outlier detection and smoothing (IQR method) ==========
    logger.info("📋 Preprocessing: Detecting and smoothing outliers...")
    outlier_count = 0
    for family in df["product_family"].unique():
        mask = df["product_family"] == family
        family_data = df.loc[mask, "stock_value"]
        
        if len(family_data) < 4:
            continue  # Not enough data for IQR
        
        Q1 = family_data.quantile(0.25)
        Q3 = family_data.quantile(0.75)
        IQR = Q3 - Q1
        
        # Define outlier bounds (allow 1.5x IQR extension)
        lower_bound = Q1 - 1.5 * IQR
        upper_bound = Q3 + 1.5 * IQR
        
        # Flag outliers
        is_outlier = (family_data < lower_bound) | (family_data > upper_bound)
        outlier_count += is_outlier.sum()
        
        # Smooth outliers by replacing with rolling median (window=3).
        # All three operands share the family sub-index, so no cross-index alignment issues.
        if is_outlier.any():
            family_series = df.loc[mask, "stock_value"].copy()
            rolling_median = family_series.rolling(window=3, center=True, min_periods=1).median()
            df.loc[mask, "stock_value"] = family_series.where(~is_outlier, rolling_median)
            logger.debug(f"   {family}: {is_outlier.sum()} outliers smoothed")
    
    if outlier_count > 0:
        logger.info(f"   Total outliers detected and smoothed: {outlier_count}")
    
    # ========== Step 3: Normalize by family magnitude ==========
    # Create normalization metadata to denormalize during forecast output
    logger.info("📋 Preprocessing: Computing family normalization factors...")
    family_stats = {}
    
    for family in df["product_family"].unique():
        mask = df["product_family"] == family
        family_values = df.loc[mask, "stock_value"]
        
        if len(family_values) == 0 or family_values.sum() == 0:
            family_stats[family] = {"mean": 1.0, "std": 1.0, "min": 0.0, "max": 1.0}
            continue
        
        mean_val = float(family_values.mean())
        std_val = float(family_values.std())
        min_val = float(family_values.min())
        max_val = float(family_values.max())
        
        # Store stats for later denormalization
        family_stats[family] = {
            "mean": mean_val,
            "std": std_val if std_val > 0 else 1.0,
            "min": min_val,
            "max": max_val,
        }
        
        # Z-score normalization: (x - mean) / std
        # This balances families so models don't bias toward larger ones
        if std_val > 0:
            df.loc[mask, "stock_value"] = (family_values - mean_val) / std_val
    
    logger.info(f"✓ Preprocessing complete: {len(df)} rows, {len(family_stats)} families normalized")
    
    # Store normalization metadata on the dataframe for later denormalization
    df.attrs["family_stats"] = family_stats
    
    return df


def _denormalize_forecast(
    forecast_value: float,
    family: Optional[str] = None,
    family_stats: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    """
    Denormalize forecast value back to original scale.
    If family_stats not provided, returns value as-is (no normalization was applied).
    Formula: y = z * std + mean
    """
    if family_stats is None or family is None or family not in family_stats:
        return forecast_value
    
    stats = family_stats[family]
    denormalized = forecast_value * stats["std"] + stats["mean"]
    
    # Ensure non-negative stock quantities
    return max(0.0, denormalized)


def _aggregate_global_and_families(
    df: pd.DataFrame,
    granularity: str,
    forecast_scope: str = FORECAST_SCOPE_NATIONAL,
    include_families: bool = True,
) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """
    Split data into:
    - global: total stock (sum across all families per date)
    - families: dict keyed by selected scope key, each with (date, stock_value)
    Includes exogenous features for enhanced modeling.

    Set include_families=False to skip the per-family loop when only the
    global aggregate is needed (e.g. during model training).
    """
    forecast_scope = _normalize_forecast_scope(forecast_scope)
    freq = MONTHLY_FREQ if granularity == "monthly" else DAILY_FREQ

    # Global aggregate — include real_count so the CV can skip synthetic test points
    agg_cols = {"stock_value": "sum", "record_count": "sum"}
    if "real_count" in df.columns:
        agg_cols["real_count"] = "sum"
    global_agg = (
        df.groupby("date", as_index=False)
        .agg(agg_cols)
        .sort_values("date")
    )
    global_agg = _fill_temporal_gaps(global_agg, freq)
    global_agg["stock_value"] = (
        pd.to_numeric(global_agg["stock_value"], errors="coerce")
        .interpolate(limit_direction="both")
        .fillna(0.0)
    )
    global_agg["record_count"] = (
        pd.to_numeric(global_agg["record_count"], errors="coerce").fillna(0.0)
    )
    if "real_count" not in global_agg.columns:
        global_agg["real_count"] = 0.0
    else:
        global_agg["real_count"] = (
            pd.to_numeric(global_agg["real_count"], errors="coerce").fillna(0.0)
        )
    global_agg = _add_inventory_features(global_agg, granularity)

    if not include_families:
        return global_agg, {}

    # Per-family aggregates with features
    families = {}
    for family in df["product_family"].unique():
        family_df = df[df["product_family"] == family].copy()
        if forecast_scope == FORECAST_SCOPE_BY_GOVERNORATE:
            non_zero_months = int((pd.to_numeric(family_df["stock_value"], errors="coerce").fillna(0.0) > 0).sum())
            if non_zero_months < 6:
                continue
        family_agg = family_df[["date", "stock_value"]].sort_values("date")
        family_agg = _fill_temporal_gaps(family_agg, freq)
        family_agg["stock_value"] = (
            pd.to_numeric(family_agg["stock_value"], errors="coerce")
            .interpolate(limit_direction="both")
            .fillna(0.0)
        )
        family_agg = _add_inventory_features(family_agg, granularity)
        families[family] = family_agg

    return global_agg, families


def _train_model_on_fold(
    model_name: str,
    y_train_fold: pd.Series,
    series_fold: pd.DataFrame,
    y_test_fold: pd.Series,
    y_test_eval: pd.Series,
    real_test_mask,
    test_size: int,
    granularity: str,
    freq: str,
    seasonal_period: int,
    fold_idx: int,
    test_dates: pd.DataFrame,
) -> Dict[str, Any] | None:
    """Evaluate one model on one CV fold. Returns a result dict or None if skipped/failed."""
    _MIN_TRAIN_POINTS: Dict[str, int] = {
        "lstm": 12, "patchtst": 12, "autogluon": 10, "prophet": 8,
    }
    min_pts = _MIN_TRAIN_POINTS.get(model_name, 2)
    if len(y_train_fold) < min_pts:
        logger.debug(
            "   %s: skip fold %d — only %d train points (need %d)",
            model_name, fold_idx + 1, len(y_train_fold), min_pts,
        )
        return None

    # A test window whose actuals sum to ~0 has no scale for WAPE/MAPE (any non-zero
    # prediction yields a meaningless multi-hundred-percent error). Skip the fold so
    # it doesn't dominate the averaged score.
    if float(np.sum(np.abs(y_test_eval.to_numpy()))) < 1e-9:
        logger.debug(
            "   %s: skip fold %d — test actuals sum to ~0 (WAPE undefined)",
            model_name, fold_idx + 1,
        )
        return None

    try:
        with tracer.start_as_current_span("inventory.model_train") as model_span:
            model_span.set_attribute(_KIND, _CHAIN)
            model_span.set_attribute("inventory.model_name", model_name)
            model_span.set_attribute("inventory.fold_index", fold_idx + 1)
            model_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "model": model_name, "fold_index": fold_idx + 1,
                "train_size": len(y_train_fold), "test_size": len(y_test_fold),
            }))
            model_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

            start_time = pd.Timestamp.now()
            y_pred = _run_forecast_for_model(
                model_key=model_name,
                y_train=y_train_fold,
                history=series_fold[["date", "stock_value"]].rename(
                    columns={"stock_value": "nb_ventes"}
                ),
                horizon=test_size,
                granularity=granularity,
                freq=freq,
                seasonal_period=seasonal_period,
            )
            y_pred = y_pred[: len(y_test_fold)]
            y_pred_eval = y_pred[real_test_mask] if real_test_mask.any() else y_pred

            mae, rmse, mape, smape = _metrics(y_test_eval.to_numpy(), y_pred_eval)
            wape = _wape(y_test_eval.to_numpy(), y_pred_eval)
            score = _weighted_model_score(mae, rmse, wape, smape)
            elapsed = (pd.Timestamp.now() - start_time).total_seconds()

            model_span.set_attribute("inventory.training_time_sec", round(elapsed, 3))
            model_span.set_attribute("inventory.mae", round(mae, 4))
            model_span.set_attribute("inventory.rmse", round(rmse, 4))
            model_span.set_attribute("inventory.wape", round(wape, 4))
            model_span.set_attribute("inventory.smape", round(smape, 4))
            model_span.set_attribute("inventory.score", round(score, 6))
            model_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "wape": round(wape, 4), "mae": round(mae, 4),
                "rmse": round(rmse, 4), "smape": round(smape, 4),
                "score": round(score, 6), "training_time_sec": round(elapsed, 3),
            }))
            model_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        logger.debug("   %s: WAPE=%.2f%%, MAE=%.2f, RMSE=%.2f", model_name, wape, mae, rmse)
        return {
            "model_name": model_name,
            "mae": mae, "rmse": rmse, "mape": mape, "smape": smape,
            "wape": wape, "score": score, "elapsed": elapsed,
            "fold_info": {
                "fold_idx": fold_idx,
                "test_start": test_dates["date"].min().strftime("%Y-%m-%d"),
                "test_end": test_dates["date"].max().strftime("%Y-%m-%d"),
            },
            "fold_detail": {
                "fold": fold_idx + 1, "wape": wape, "mae": mae,
                "rmse": rmse, "score": score,
            },
        }
    except Exception as exc:
        logger.warning("   %s failed on fold %d: %s", model_name, fold_idx + 1, exc)
        return None


def train_inventory_models(
    db: Session,
    horizon: int,
    enable_generative: bool = True,
    selected_models: List[str] | None = None,
    granularity: str = "monthly",
    forecast_target: str = FORECAST_TARGET_STOCK,
    forecast_scope: str = FORECAST_SCOPE_NATIONAL,
    service_type: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Train multiple models on inventory history and return results ranked by weighted score.
    Returns list of dicts with keys: model, mae, rmse, mape, smape, wape, wape_std, score, training_time_sec, yhat
    
    Data preprocessing pipeline:
    1. Forward-fill + linear interpolation for missing values
    2. IQR-based outlier smoothing
    3. Z-score normalization by family (prevents under-weighting smaller families)
    """
    with tracer.start_as_current_span("inventory.training") as root_span:
        root_span.set_attribute(_KIND, _CHAIN)
        root_span.set_attribute("inventory.granularity", granularity)
        root_span.set_attribute("inventory.horizon", horizon)
        root_span.set_attribute("inventory.forecast_target", forecast_target)
        root_span.set_attribute("inventory.forecast_scope", forecast_scope)
        root_span.set_attribute("inventory.enable_generative", enable_generative)
        root_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
            "granularity": granularity, "horizon": horizon,
            "forecast_target": forecast_target, "forecast_scope": forecast_scope,
            "enable_generative": enable_generative,
        }))
        root_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

        forecast_target = _normalize_forecast_target(forecast_target)
        forecast_scope = _normalize_forecast_scope(forecast_scope)
        if granularity == "monthly":
            horizon = min(horizon, settings.FORECAST_HORIZON_MONTHLY_DEFAULT or 6)

        with tracer.start_as_current_span("inventory.data_load") as load_span:
            load_span.set_attribute(_KIND, _CHAIN)
            load_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "granularity": granularity, "forecast_target": forecast_target,
                "forecast_scope": forecast_scope,
            }))
            load_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            df = load_inventory_history(
                db,
                granularity=granularity,
                forecast_target=forecast_target,
                forecast_scope=forecast_scope,
                service_type=service_type,
            )
            if df.empty:
                raise ValueError("No inventory history available for training")
            load_span.set_attribute("inventory.row_count", len(df))
            load_span.set_attribute("inventory.family_count", int(df["product_family"].nunique()))
            load_span.set_attribute("inventory.date_range_start", str(df["date"].min()))
            load_span.set_attribute("inventory.date_range_end", str(df["date"].max()))
            load_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "row_count": len(df), "family_count": int(df["product_family"].nunique()),
                "date_range_start": str(df["date"].min()), "date_range_end": str(df["date"].max()),
            }))
            load_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        # Snapshot raw data before z-score normalization — CV must run on real stock scale.
        # Summing per-family z-scores into a global aggregate produces a near-zero series,
        # which makes WAPE undefined (tiny denominator) and model selection meaningless.
        raw_df = df.copy()

        with tracer.start_as_current_span("inventory.data_preprocessing") as prep_span:
            prep_span.set_attribute(_KIND, _CHAIN)
            prep_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "granularity": granularity, "row_count": len(df),
            }))
            prep_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            df = _preprocess_inventory_data(df, granularity)
            families_normalized = len(df.attrs.get("family_stats", {}))
            prep_span.set_attribute("inventory.families_normalized", families_normalized)
            prep_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "families_normalized": families_normalized,
            }))
            prep_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        # Use raw (non-normalized) global aggregate for CV so WAPE reflects real stock units
        global_agg, _ = _aggregate_global_and_families(
            raw_df, granularity, forecast_scope=forecast_scope, include_families=False
        )
        freq = MONTHLY_FREQ if granularity == "monthly" else DAILY_FREQ
        series = global_agg.sort_values("date")
        # Drop trailing empty periods (mainly the demand target's future-dated zero
        # months) so walk-forward CV scores real demand instead of exploding WAPE on
        # empty tail months. No-op for the stock target (non-zero tail).
        series = _trim_trailing_zero_periods(series, value_col="stock_value")
        if len(series) < 6:
            raise ValueError(
                f"Insufficient inventory history. Need at least 6 months; got {len(series)}"
            )
        y_train = series["stock_value"].astype(float)

        with tracer.start_as_current_span("inventory.cv_setup") as cv_span:
            cv_span.set_attribute(_KIND, _CHAIN)
            cv_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "total_history_points": len(y_train), "horizon": horizon,
                "date_range_start": series["date"].min().strftime("%Y-%m-%d"),
                "date_range_end": series["date"].max().strftime("%Y-%m-%d"),
            }))
            cv_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            test_size = max(1, min(horizon, len(y_train) - 1))
            max_feasible_splits = (len(y_train) - 1) // test_size
            if max_feasible_splits < 2:
                raise ValueError(
                    f"Insufficient inventory history for walk-forward cross-validation. "
                    f"Need at least {2 * test_size + 1} data points for 2 folds with "
                    f"test_size={test_size}; got {len(y_train)}. "
                    f"Upload more historical data or reduce the forecast horizon."
                )
            n_splits = min(3, max_feasible_splits)
            tscv = TimeSeriesSplit(n_splits=n_splits, test_size=test_size, gap=0)
            fold_splits = list(tscv.split(y_train))

            cv_span.set_attribute("inventory.total_history_points", len(y_train))
            cv_span.set_attribute("inventory.test_size_per_fold", test_size)
            cv_span.set_attribute("inventory.n_splits", n_splits)
            cv_span.set_attribute("inventory.date_range_start", series["date"].min().strftime("%Y-%m-%d"))
            cv_span.set_attribute("inventory.date_range_end", series["date"].max().strftime("%Y-%m-%d"))
            cv_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "n_splits": n_splits, "test_size": test_size,
                "date_range_start": series["date"].min().strftime("%Y-%m-%d"),
                "date_range_end": series["date"].max().strftime("%Y-%m-%d"),
            }))
            cv_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        logger.info(f"📊 Inventory Walk-Forward Cross-Validation Setup:")
        logger.info(f"   Total history points: {len(y_train)}")
        logger.info(f"   Test size per fold (horizon): {test_size}")
        logger.info(f"   Number of folds: {len(fold_splits)}")
        logger.info(f"   Date range: {series['date'].min().strftime('%Y-%m-%d')} to {series['date'].max().strftime('%Y-%m-%d')}")
        logger.info(f"\n   Fold-by-Fold Date Ranges:")

        for fold_idx, (train_idx, test_idx) in enumerate(fold_splits):
            train_dates = series.iloc[train_idx]
            test_dates = series.iloc[test_idx]
            logger.info(
                f"   Fold {fold_idx + 1}: Train [{train_dates['date'].min().strftime('%Y-%m-%d')} → "
                f"{train_dates['date'].max().strftime('%Y-%m-%d')}] | "
                f"Test [{test_dates['date'].min().strftime('%Y-%m-%d')} → {test_dates['date'].max().strftime('%Y-%m-%d')}]"
            )

        fold_results: Dict[str, Dict[str, Any]] = {}
        fold_details: Dict[str, List[Dict[str, Any]]] = {}
        model_errors: Dict[str, List[str]] = {}

        models_to_run = resolve_inventory_models(selected_models, enable_generative)

        for model_name in models_to_run:
            model_errors[model_name] = []

        root_span.set_attribute("inventory.models_to_run", ",".join(models_to_run))
        root_span.set_attribute("inventory.models_input", json.dumps(models_to_run))

        # Surface generative models that aren't installed in this deployment, so their
        # silent absence is visible in the trace rather than just a smaller count.
        if enable_generative:
            installed_gen = set(get_available_generative_models())
            unavailable_gen = [m for m in GENERATIVE_MODELS if m not in installed_gen]
            if unavailable_gen:
                root_span.set_attribute("inventory.generative_unavailable", json.dumps(unavailable_gen))
                logger.info("Generative models not installed (skipped): %s", unavailable_gen)

        results: List[Dict[str, Any]] = []
        seasonal_period = (
            MONTHLY_SEASONAL_PERIOD
            if granularity == "monthly"
            else DAILY_SEASONAL_PERIOD
        )

        logger.info(f"\n🔄 Starting Walk-Forward Evaluation ({len(fold_splits)} folds):\n")

        for fold_idx, (train_idx, test_idx) in enumerate(fold_splits):
            y_train_fold = y_train.iloc[train_idx].copy()
            y_test_fold = y_train.iloc[test_idx].copy()
            series_fold = series.iloc[train_idx].copy()
            test_dates = series.iloc[test_idx]

            # Evaluate metrics only on real (non-synthetic) test points so synthetic
            # history rows don't penalise models for not predicting synthetic noise.
            # Falls back to all test points when the entire test window is synthetic.
            real_test_mask = (series.iloc[test_idx]["real_count"] > 0).values
            if real_test_mask.any():
                y_test_eval = y_test_fold.iloc[real_test_mask]
                real_test_count = int(real_test_mask.sum())
            else:
                y_test_eval = y_test_fold
                real_test_count = 0

            logger.info(
                f"📂 Fold {fold_idx + 1}/{len(fold_splits)}: "
                f"Train {len(y_train_fold)} points | Test {len(y_test_fold)} points "
                f"({real_test_count} real) | "
                f"Testing on {test_dates['date'].min().strftime('%Y-%m-%d')} to {test_dates['date'].max().strftime('%Y-%m-%d')}"
            )

            with tracer.start_as_current_span("inventory.fold") as fold_span:
                fold_span.set_attribute(_KIND, _CHAIN)
                fold_span.set_attribute("inventory.fold_index", fold_idx + 1)
                fold_span.set_attribute("inventory.fold_train_size", len(y_train_fold))
                fold_span.set_attribute("inventory.fold_test_size", len(y_test_fold))
                fold_span.set_attribute("inventory.fold_test_start", test_dates["date"].min().strftime("%Y-%m-%d"))
                fold_span.set_attribute("inventory.fold_test_end", test_dates["date"].max().strftime("%Y-%m-%d"))
                fold_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                    "fold_index": fold_idx + 1, "train_size": len(y_train_fold),
                    "test_size": len(y_test_fold),
                    "test_start": test_dates["date"].min().strftime("%Y-%m-%d"),
                    "test_end": test_dates["date"].max().strftime("%Y-%m-%d"),
                }))
                fold_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

                # Run all models for this fold in parallel. Each model is independent —
                # they share read-only copies of the fold data and write to separate keys.
                # Each future needs its own copied context; reusing the same Context
                # object across concurrent workers triggers "context is already entered".
                n_workers = min(len(models_to_run), 6)
                with ThreadPoolExecutor(max_workers=n_workers) as pool:
                    futures = {
                        pool.submit(
                            contextvars.copy_context().run,
                            _train_model_on_fold,
                            model_name,
                            y_train_fold,
                            series_fold,
                            y_test_fold,
                            y_test_eval,
                            real_test_mask,
                            test_size,
                            granularity,
                            freq,
                            seasonal_period,
                            fold_idx,
                            test_dates,
                        ): model_name
                        for model_name in models_to_run
                    }
                    for future in as_completed(futures):
                        model_name = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:
                            logger.warning("   %s failed on fold %d: %s", model_name, fold_idx + 1, exc)
                            model_errors.setdefault(model_name, []).append(str(exc))
                            continue
                        if result is None:
                            model_errors.setdefault(model_name, []).append(
                                f"Fold {fold_idx + 1} produced no usable metrics"
                            )
                            continue

                        if model_name not in fold_results:
                            fold_results[model_name] = {
                                "mae": [], "rmse": [], "mape": [], "smape": [],
                                "wape": [], "score": [], "elapsed": [], "fold_info": []
                            }
                            fold_details[model_name] = []

                        fold_results[model_name]["mae"].append(result["mae"])
                        fold_results[model_name]["rmse"].append(result["rmse"])
                        fold_results[model_name]["mape"].append(result["mape"])
                        fold_results[model_name]["smape"].append(result["smape"])
                        fold_results[model_name]["wape"].append(result["wape"])
                        fold_results[model_name]["score"].append(result["score"])
                        fold_results[model_name]["elapsed"].append(result["elapsed"])
                        fold_results[model_name]["fold_info"].append(result["fold_info"])
                        fold_details[model_name].append(result["fold_detail"])

        # ========== Aggregate Metrics Across Folds with Recency Weighting ==========
        recency_weights = np.linspace(0.5, 1.5, len(fold_splits))
        recency_weights = recency_weights / recency_weights.sum()

        logger.info(f"\n📊 Walk-Forward Validation Results ({len(fold_splits)} folds, recency-weighted):\n")
        logger.info(f"   Recency weights: {recency_weights}")

        for model_name in sorted(fold_results.keys()):
            with tracer.start_as_current_span("inventory.metrics_aggregation") as agg_span:
                agg_span.set_attribute(_KIND, _CHAIN)
                agg_span.set_attribute("inventory.model_name", model_name)
                agg_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                    "model": model_name,
                    "completed_folds": len(fold_results[model_name]["mae"]),
                }))
                agg_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
                metrics = fold_results[model_name]

                logger.debug(f"   {model_name}: wape_len={len(metrics['wape'])}, fold_splits={len(fold_splits)}")

                num_completed_folds = len(metrics["mae"])
                if num_completed_folds == 0:
                    logger.warning(f"   Skipping {model_name}: no successful folds")
                    agg_span.set_attribute("inventory.skipped", True)
                    continue
                if num_completed_folds != len(fold_splits):
                    logger.warning(f"   {model_name}: only {num_completed_folds}/{len(fold_splits)} folds succeeded")
                    recency_weights_model = np.linspace(0.5, 1.5, num_completed_folds)
                    recency_weights_model = recency_weights_model / recency_weights_model.sum()
                else:
                    recency_weights_model = recency_weights

                mae_values = np.array([float(v) for v in metrics["mae"]], dtype=np.float64)
                rmse_values = np.array([float(v) for v in metrics["rmse"]], dtype=np.float64)
                mape_values = np.array([float(v) for v in metrics["mape"]], dtype=np.float64)
                smape_values = np.array([float(v) for v in metrics["smape"]], dtype=np.float64)
                wape_values = np.array([float(v) for v in metrics["wape"]], dtype=np.float64)
                score_values = np.array([float(v) for v in metrics["score"]], dtype=np.float64)
                elapsed_values = np.array([float(v) for v in metrics["elapsed"]], dtype=np.float64)

                avg_mae = float(np.average(mae_values, weights=recency_weights_model))
                avg_rmse = float(np.average(rmse_values, weights=recency_weights_model))
                avg_mape = float(np.average(mape_values, weights=recency_weights_model))
                avg_smape = float(np.average(smape_values, weights=recency_weights_model))
                avg_wape = float(np.average(wape_values, weights=recency_weights_model))
                avg_score = float(np.average(score_values, weights=recency_weights_model))
                avg_elapsed = float(np.mean(elapsed_values))
                wape_std = float(np.std(wape_values))
                score_std = float(np.std(score_values))
                best_fold_idx = int(np.argmin(wape_values))
                worst_fold_idx = int(np.argmax(wape_values))
                best_wape = float(wape_values[best_fold_idx])
                worst_wape = float(wape_values[worst_fold_idx])

                agg_span.set_attribute("inventory.completed_folds", num_completed_folds)
                agg_span.set_attribute("inventory.avg_wape", round(avg_wape, 4))
                agg_span.set_attribute("inventory.wape_std", round(wape_std, 4))
                agg_span.set_attribute("inventory.avg_mae", round(avg_mae, 4))
                agg_span.set_attribute("inventory.avg_rmse", round(avg_rmse, 4))
                agg_span.set_attribute("inventory.avg_score", round(avg_score, 6))
                agg_span.set_attribute("inventory.best_fold", best_fold_idx + 1)
                agg_span.set_attribute("inventory.best_fold_wape", round(best_wape, 4))
                agg_span.set_attribute("inventory.worst_fold", worst_fold_idx + 1)
                agg_span.set_attribute("inventory.worst_fold_wape", round(worst_wape, 4))
                agg_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                    "model": model_name, "avg_wape": round(avg_wape, 4),
                    "wape_std": round(wape_std, 4), "avg_mae": round(avg_mae, 4),
                    "avg_rmse": round(avg_rmse, 4), "avg_score": round(avg_score, 6),
                    "completed_folds": num_completed_folds,
                    "best_fold_wape": round(best_wape, 4), "worst_fold_wape": round(worst_wape, 4),
                }))
                agg_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

                results.append({
                    "model": model_name,
                    "mae": avg_mae,
                    "rmse": avg_rmse,
                    "mape": avg_mape,
                    "smape": avg_smape,
                    "wape": avg_wape,
                    "score": avg_score,
                    "training_time_sec": avg_elapsed,
                    "fold_count": len(fold_splits),
                    "wape_std": wape_std,
                    "score_std": score_std,
                    "best_fold": best_fold_idx + 1,
                    "best_fold_wape": best_wape,
                    "worst_fold": worst_fold_idx + 1,
                    "worst_fold_wape": worst_wape,
                    "yhat": [],
                })

                logger.info(f"✓ {model_name}:")
                logger.info(f"   Weighted avg WAPE: {avg_wape:.2f}% (stability: ±{wape_std:.2f}%)")
                logger.info(f"   Best fold (#{best_fold_idx + 1}): {best_wape:.2f}% | Worst fold (#{worst_fold_idx + 1}): {worst_wape:.2f}%")
                logger.info(f"   Per-fold WAPE: {', '.join([f'{w:.2f}%' for w in wape_values])}")
                logger.info(f"   Weighted score: {avg_score:.4f} (±{score_std:.4f}), MAE={avg_mae:.2f}, RMSE={avg_rmse:.2f}\n")

        if not results:
            raise ValueError("No inventory forecasting models could be trained")

        results_sorted = sorted(results, key=lambda x: x["score"])
        failed_results: List[Dict[str, Any]] = []
        for model_name in models_to_run:
            if model_name in fold_results:
                continue
            failed_results.append({
                "model": model_name,
                "status": "failed",
                "error_message": model_errors.get(model_name, ["Model training failed before metrics could be computed"])[-1],
                "mae": None,
                "rmse": None,
                "mape": None,
                "smape": None,
                "wape": None,
                "score": None,
                "training_time_sec": None,
                "fold_count": 0,
                "wape_std": None,
                "score_std": None,
                "best_fold": None,
                "best_fold_wape": None,
                "worst_fold": None,
                "worst_fold_wape": None,
                "yhat": [],
            })

        final_results = results_sorted + failed_results
        best = results_sorted[0]
        root_span.set_attribute("inventory.best_model", best["model"])
        root_span.set_attribute("inventory.best_wape", round(best["wape"], 4))
        root_span.set_attribute("inventory.models_evaluated", len(final_results))
        root_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
            "best_model": best["model"], "best_wape": round(best["wape"], 4),
            "models_evaluated": len(final_results),
            "models": [
                {
                    "model": r["model"],
                    "wape": round(r["wape"], 4) if isinstance(r.get("wape"), (int, float)) else None,
                    "status": r.get("status", "completed"),
                }
                for r in final_results
            ],
        }))
        root_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
        return final_results


def _select_forecast_model(results: List[Dict[str, Any]]) -> Optional[str]:
    """
    Pick the model used for serving forecasts.

    The top score is still reported as the metric winner, but if a near-tied
    model is more expressive than a flat baseline, prefer that model for the
    actual forecast so the output is not a constant line.
    """
    if not results:
        return None

    best_score = float(results[0]["score"])
    score_ceiling = best_score * 1.10
    preference_order = ["patchtst", "autogluon", "seasonal_naive", "lstm", "prophet", "naive_last"]

    eligible_models = {
        item["model"]
        for item in results
        if float(item.get("score", float("inf"))) <= score_ceiling
    }

    for candidate in preference_order:
        if candidate in eligible_models:
            return candidate

    return results[0]["model"]


def generate_inventory_forecast(
    db: Session,
    best_model_name: str,
    horizon: int,
    granularity: str = "monthly",
    scope: str = "global",
    family: Optional[str] = None,
    forecast_target: str = FORECAST_TARGET_STOCK,
    forecast_scope: str = FORECAST_SCOPE_NATIONAL,
    service_type: str | None = None,
) -> Dict[str, Any]:
    """
    Generate inventory forecast using best model.
    Supports `scope` = 'global', 'per_family', or 'both'.
    
    Data preprocessing applied:
    1. Forward-fill + linear interpolation for missing values
    2. IQR-based outlier smoothing
    3. Z-score normalization by family (forecasts denormalized before output)
    
    Returns dict with keys: historical, forecast (global and/or per_family), metadata
    """
    with tracer.start_as_current_span("inventory.forecast_pipeline") as root_span:
        root_span.set_attribute(_KIND, _CHAIN)
        root_span.set_attribute("inventory.granularity", granularity)
        root_span.set_attribute("inventory.horizon", horizon)
        root_span.set_attribute("inventory.scope", scope)
        root_span.set_attribute("inventory.forecast_target", forecast_target)
        root_span.set_attribute("inventory.forecast_scope", forecast_scope)
        root_span.set_attribute("inventory.best_model", best_model_name)

        forecast_target = _normalize_forecast_target(forecast_target)
        forecast_scope = _normalize_forecast_scope(forecast_scope)
        if granularity == "monthly":
            horizon = min(horizon, settings.FORECAST_HORIZON_MONTHLY_DEFAULT or 6)

        with tracer.start_as_current_span("inventory.data_load") as load_span:
            load_span.set_attribute(_KIND, _CHAIN)
            load_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "granularity": granularity, "forecast_target": forecast_target,
                "forecast_scope": forecast_scope,
            }))
            load_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            df = load_inventory_history(
                db,
                granularity=granularity,
                forecast_target=forecast_target,
                forecast_scope=forecast_scope,
                service_type=service_type,
            )
            if df.empty:
                raise ValueError("No inventory history available")
            load_span.set_attribute("inventory.row_count", len(df))
            load_span.set_attribute("inventory.family_count", int(df["product_family"].nunique()))
            load_span.set_attribute("inventory.date_range_start", str(df["date"].min()))
            load_span.set_attribute("inventory.date_range_end", str(df["date"].max()))
            load_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "row_count": len(df), "family_count": int(df["product_family"].nunique()),
                "date_range_start": str(df["date"].min()), "date_range_end": str(df["date"].max()),
            }))
            load_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        raw_df = df.copy()
        raw_global_agg, _ = _aggregate_global_and_families(
            raw_df, granularity, forecast_scope=forecast_scope, include_families=False
        )
        raw_series = raw_global_agg.sort_values("date")

        with tracer.start_as_current_span("inventory.data_preprocessing") as prep_span:
            prep_span.set_attribute(_KIND, _CHAIN)
            prep_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "granularity": granularity, "row_count": len(df),
            }))
            prep_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            df = _preprocess_inventory_data(df, granularity)
            family_stats = df.attrs.get("family_stats", {})
            prep_span.set_attribute("inventory.families_normalized", len(family_stats))
            prep_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                "families_normalized": len(family_stats),
            }))
            prep_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        global_agg, families = _aggregate_global_and_families(df, granularity, forecast_scope=forecast_scope)
        if len(global_agg) < 6:
            raise ValueError("Insufficient inventory history for forecasting")

        freq = MONTHLY_FREQ if granularity == "monthly" else DAILY_FREQ
        series = global_agg.sort_values("date")
        y_train = series["stock_value"].astype(float)

        model_key = _normalize_model_key(best_model_name)
        seasonal_period = (
            MONTHLY_SEASONAL_PERIOD
            if granularity == "monthly"
            else DAILY_SEASONAL_PERIOD
        )

        def _build_forecast_from_series(
            _series: pd.DataFrame,
            _horizon: int,
            _family: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            """
            Build forecast list with denormalization.
            If family and family_stats provided, denormalizes predictions back to original scale.
            """
            _y = _series["stock_value"].astype(float)
            # Short series can't fit models like autogluon/lstm/prophet — downgrade to a
            # naive model instead of raising and dropping this family from the aggregate.
            _effective_model = _pick_effective_model(model_key, len(_y), seasonal_period)
            if _effective_model != model_key:
                logger.info(
                    "Family '%s': %d history points too short for '%s' — downgrading to '%s'",
                    _family, len(_y), model_key, _effective_model,
                )
                _cur = trace.get_current_span()
                _cur.set_attribute("inventory.model_downgraded", True)
                _cur.set_attribute("inventory.model_downgraded_from", model_key)
                _cur.set_attribute("inventory.model_downgraded_to", _effective_model)
            _forecast_vals = _run_forecast_for_model(
                model_key=_effective_model,
                y_train=_y,
                history=_series[["date", "stock_value"]].rename(columns={"stock_value": "nb_ventes"}),
                horizon=_horizon,
                granularity=granularity,
                freq=freq,
                seasonal_period=seasonal_period,
            )

            # Denormalize normalized forecast values
            _forecast_vals_denorm = [
                _denormalize_forecast(val, family=_family, family_stats=family_stats)
                for val in _forecast_vals
            ]

            last_hist_date = pd.to_datetime(_series["date"].max())
            start_date = last_hist_date + (
                pd.offsets.MonthBegin(1) if granularity == "monthly" else timedelta(days=1)
            )
            fc_dates = pd.date_range(start=start_date, periods=_horizon, freq=freq)

            # Use denormalized std for confidence intervals
            sigma_local = float(
                max(1.0, np.std(_forecast_vals_denorm) if len(_forecast_vals_denorm) > 1 else 1.0)
            )
            fc_list: List[Dict[str, Any]] = []
            for d, p in zip(fc_dates, _forecast_vals_denorm):
                lower = max(0.0, float(p - 1.28 * sigma_local))
                upper = float(p + 1.28 * sigma_local)
                fc_list.append({"date": d.strftime("%Y-%m-%d"), "value": float(p), "lower_bound": lower, "upper_bound": upper})
            return fc_list

        # Historical tail (global, raw/original scale for chart readability)
        hist_tail = raw_series.tail(min(120, len(raw_series)))
        historical = [
            {"date": d.strftime("%Y-%m-%d"), "value": float(v)}
            for d, v in zip(hist_tail["date"], hist_tail["stock_value"])
        ]

        # Compute data source mix from raw_df (before preprocessing alters values)
        total_real = int(raw_df["real_count"].sum()) if "real_count" in raw_df.columns else 0
        total_simulated = int(raw_df["simulated_count"].sum()) if "simulated_count" in raw_df.columns else 0

        result: Dict[str, Any] = {
            "historical": historical,
            "forecast": {},
            "metadata": {
                "model_used": model_key,
                "generation_type": "model_generated",
                "is_fallback": False,
                "scope": scope,
                "forecast_target": forecast_target,
                "forecast_scope": forecast_scope,
                "family": family or None,
                "data_source_real_count": total_real,
                "data_source_simulated_count": total_simulated,
            },
        }

        # scope='global': run the model ONCE on the raw global aggregate and return early.
        # raw_series is already in original scale so _denormalize_forecast is a no-op (family=None).
        # This bypasses the per-family loop entirely (no N × model fits).
        if scope == "global":
            with tracer.start_as_current_span("inventory.global_forecast") as agg_span:
                agg_span.set_attribute(_KIND, _CHAIN)
                agg_span.set_attribute("inventory.method", "direct_global")
                agg_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                    "model": model_key, "horizon": horizon, "history_len": len(raw_series),
                }))
                agg_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

                global_fc = _build_forecast_from_series(raw_series, horizon, _family=None)

                last_hist = historical[-1]["value"] if historical else 1.0
                last_fc = global_fc[-1]["value"] if global_fc else last_hist
                change_pct = ((last_fc - last_hist) / max(1.0, last_hist)) * 100.0

                agg_span.set_attribute("inventory.trend", "hausse" if change_pct >= 0 else "baisse")
                agg_span.set_attribute("inventory.change_pct", round(change_pct, 2))
                agg_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                    "trend": "hausse" if change_pct >= 0 else "baisse",
                    "change_pct": round(change_pct, 2),
                    "forecast_points": len(global_fc),
                }))
                agg_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

            result["forecast"]["global"] = {
                "historical": historical,
                "forecast": global_fc,
                "metadata": {
                    "model_used": model_key,
                    "generation_type": "model_generated",
                    "is_fallback": False,
                    "scope": "global",
                },
            }
            result["metadata"].update({
                "trend": "hausse" if change_pct >= 0 else "baisse",
                "change_pct": round(change_pct, 2),
                "partial_results": False,
                "failed_families": [],
            })
            return result

        # scope='per_family' or 'both': loop over each family.
        # The normalized global aggregate cannot be inverted to original scale directly,
        # so the global total is derived by summing individually-denormalized family forecasts.
        per_family_forecasts: Dict[str, Any] = {}
        global_fc_by_date: Dict[str, float] = {}
        failed_families: List[str] = []
        for fam_name, fam_df in families.items():
            if len(fam_df) < 2:
                continue
            if family and fam_name != family:
                continue
            try:
                with tracer.start_as_current_span("inventory.family_forecast") as fam_span:
                    fam_span.set_attribute(_KIND, _CHAIN)
                    fam_span.set_attribute("inventory.family_name", fam_name)
                    fam_span.set_attribute("inventory.family_history_len", len(fam_df))
                    fam_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                        "family": fam_name, "history_len": len(fam_df),
                        "model": best_model_name, "horizon": horizon,
                    }))
                    fam_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

                    fam_hist_tail = fam_df.tail(min(120, len(fam_df)))
                    fam_historical_values = [
                        _denormalize_forecast(float(v), family=fam_name, family_stats=family_stats)
                        for v in fam_hist_tail["stock_value"]
                    ]
                    fam_historical = [
                        {"date": d.strftime("%Y-%m-%d"), "value": float(v)}
                        for d, v in zip(fam_hist_tail["date"], fam_historical_values)
                    ]
                    fam_fc = _build_forecast_from_series(fam_df, horizon, _family=fam_name)
                    fam_span.set_attribute("inventory.family_forecast_points", len(fam_fc))
                    fam_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                        "family": fam_name, "forecast_points": len(fam_fc),
                        "forecast_dates": [p["date"] for p in fam_fc[:3]] + (["..."] if len(fam_fc) > 3 else []),
                        "first_value": round(fam_fc[0]["value"], 2) if fam_fc else None,
                        "last_value": round(fam_fc[-1]["value"], 2) if fam_fc else None,
                    }))
                    fam_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

                fam_effective_model = _pick_effective_model(model_key, len(fam_df), seasonal_period)
                fam_downgraded = fam_effective_model != model_key
                per_family_forecasts[fam_name] = {
                    "historical": fam_historical,
                    "forecast": fam_fc,
                    "metadata": {
                        "model_used": fam_effective_model,
                        "generation_type": "model_generated",
                        "is_fallback": fam_downgraded,
                        "downgraded_from": model_key if fam_downgraded else None,
                        "downgrade_reason": (
                            f"only {len(fam_df)} history points" if fam_downgraded else None
                        ),
                        "scope": "per_family",
                        "family": fam_name,
                    },
                }
                for point in fam_fc:
                    global_fc_by_date[point["date"]] = global_fc_by_date.get(point["date"], 0.0) + point["value"]
            except Exception as exc:
                logger.warning(
                    "Per-family forecast failed for '%s', excluded from global sum: %s",
                    fam_name, exc, exc_info=True,
                )
                failed_families.append(fam_name)
                continue

        root_span.set_attribute("inventory.families_succeeded", len(per_family_forecasts))
        root_span.set_attribute("inventory.families_failed", len(failed_families))
        if failed_families:
            root_span.set_attribute("inventory.failed_families", ",".join(failed_families))

        if failed_families:
            result["metadata"]["partial_results"] = True
            result["metadata"]["failed_families"] = failed_families
        else:
            result["metadata"]["partial_results"] = False
            result["metadata"]["failed_families"] = []

        if scope in ("global", "both"):
            with tracer.start_as_current_span("inventory.global_forecast_aggregation") as agg_span:
                agg_span.set_attribute(_KIND, _CHAIN)
                agg_span.set_attribute("inventory.contributing_families", len(per_family_forecasts))
                agg_span.set_attribute("inventory.forecast_dates", len(global_fc_by_date))
                agg_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                    "contributing_families": list(per_family_forecasts.keys()),
                    "forecast_dates": sorted(global_fc_by_date.keys())[:3],
                    "scope": scope,
                }))
                agg_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

                global_fc_values = [v for _, v in sorted(global_fc_by_date.items())]
                sigma_global = float(max(1.0, np.std(global_fc_values) if len(global_fc_values) > 1 else 1.0))
                global_fc = [
                    {
                        "date": date,
                        "value": value,
                        "lower_bound": max(0.0, value - 1.28 * sigma_global),
                        "upper_bound": value + 1.28 * sigma_global,
                    }
                    for date, value in sorted(global_fc_by_date.items())
                ]

                last_hist = historical[-1]["value"] if historical else 1.0
                last_fc = global_fc[-1]["value"] if global_fc else last_hist
                change_pct = ((last_fc - last_hist) / max(1.0, last_hist)) * 100.0

                agg_span.set_attribute("inventory.trend", "hausse" if change_pct >= 0 else "baisse")
                agg_span.set_attribute("inventory.change_pct", round(change_pct, 2))
                agg_span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
                    "trend": "hausse" if change_pct >= 0 else "baisse",
                    "change_pct": round(change_pct, 2),
                    "forecast_points": len(global_fc_by_date),
                    "contributing_families": len(per_family_forecasts),
                }))
                agg_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

            result["forecast"]["global"] = {
                "historical": historical,
                "forecast": global_fc,
                "metadata": {
                    "model_used": model_key,
                    "generation_type": "model_generated",
                    "is_fallback": False,
                    "scope": "global",
                },
            }
            result["metadata"].update({"trend": "hausse" if change_pct >= 0 else "baisse", "change_pct": round(change_pct, 2)})

        if scope in ("per_family", "both"):
            result["forecast"]["per_family"] = per_family_forecasts

        return result
