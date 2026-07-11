from __future__ import annotations

import math
import re
import time
import json
import logging
import os
import tempfile
import difflib
import unicodedata
import shutil
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

import httpx
import mlflow
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import OneHotEncoder
from sqlalchemy import text
from sqlalchemy.orm import Session
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX
from xgboost import XGBRegressor
from prophet import Prophet

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

from app.core.config import settings
from app.core.tracing import get_tracer
from app.services.ollama_client import ollama_client

tracer = get_tracer(__name__)
_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value

logger = logging.getLogger(__name__)
_CMDSTAN_READY = False

CLASSIC_MODELS = [
    "naive_last",
    "seasonal_naive",
    "prophet",
    "sarima",
    "xgboost",
    "lstm",
    "exp_smoothing",
    "linear_regression",
]
# Generative model family includes local Ollama models and optional local libraries.
# Moirai is disabled in this deployment because the compose stack does not run that service.
GENERATIVE_MODELS = ["patchtst", "autogluon", "chronos", "moirai", "timesfm"]
ALL_MODELS = CLASSIC_MODELS + GENERATIVE_MODELS
DAILY_FREQ = "D"
MONTHLY_FREQ = "MS"
DAILY_SEASONAL_PERIOD = 7
MONTHLY_SEASONAL_PERIOD = 12

# Ramadan windows covering training data (2023+) and max 12-month forecast horizon
_RAMADAN_WINDOWS: list[tuple[str, str]] = [
    ("2023-03-23", "2023-04-21"),
    ("2024-03-11", "2024-04-09"),
    ("2025-03-01", "2025-03-30"),
    ("2026-02-18", "2026-03-19"),
    ("2027-02-07", "2027-03-08"),
]

# Tunisian fixed public holidays: (month, day)
_TN_FIXED_HOLIDAYS: list[tuple[int, int]] = [
    (1, 1),   # Nouvel An
    (1, 14),  # Journée de la Révolution
    (3, 20),  # Fête de l'Indépendance
    (4, 9),   # Journée des Martyrs
    (5, 1),   # Fête du Travail
    (7, 25),  # Fête Nationale
    (8, 13),  # Journée de la Femme
    (10, 15), # Journée de l'Evacuation
]


def _make_ramadan_mask(dates: pd.Series) -> pd.Series:
    """Return a 0/1 Series: 1 for dates that fall within a Ramadan window."""
    mask = pd.Series(False, index=dates.index)
    for start_str, end_str in _RAMADAN_WINDOWS:
        start = pd.Timestamp(start_str)
        end = pd.Timestamp(end_str)
        mask |= (dates >= start) & (dates <= end)
    return mask.astype(int)


def _make_holiday_mask(dates: pd.Series) -> pd.Series:
    """Return a 0/1 Series: 1 for Tunisian fixed public holidays."""
    md = list(zip(dates.dt.month, dates.dt.day))
    return pd.Series(
        [1 if (m, d) in _TN_FIXED_HOLIDAYS else 0 for m, d in md],
        index=dates.index,
    )


def _normalize_model_key(name: str | None) -> str:
    """Normalize model names from UI/API to canonical keys used by backend."""
    if not name:
        return "naive_last"
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized


def get_available_generative_models() -> list[str]:
    """
    Return generative models that can actually run in this deployment.
    Library-based: checked via import. HTTP-based: checked via config URL presence.
    """
    available: list[str] = []

    # PatchTST — via neuralforecast
    try:
        from neuralforecast.models import PatchTST  # type: ignore # noqa: F401
        available.append("patchtst")
    except Exception:
        pass

    # AutoGluon TimeSeries
    try:
        import autogluon.timeseries  # type: ignore  # noqa: F401
        available.append("autogluon")
    except Exception:
        pass

    # Chronos — Amazon foundation model (chronos-forecasting package)
    try:
        from chronos import ChronosPipeline  # type: ignore # noqa: F401
        available.append("chronos")
    except Exception:
        pass

    # Moirai — HTTP service (requires MOIRAI_API_URL to be configured)
    if settings.MOIRAI_API_URL:
        available.append("moirai")

    # TimesFM — HTTP service (requires TIMESFM_API_URL to be configured)
    if settings.TIMESFM_API_URL:
        available.append("timesfm")

    return available


@dataclass
class FeatureImportance:
    """Feature importance for a trained model"""
    feature: str
    importance: float
    normalized_importance: float  # 0-100 scale


BIAS_PENALTY_WEIGHT: float = 0.5  # λ in: composite = MAPE + λ × |bias_pct|

@dataclass
class ModelRunResult:
    model: str
    mae: float
    rmse: float
    mape: float
    smape: float
    bias: float
    bias_pct: float       # |bias| as % of mean demand — scale-free, comparable across products
    composite_score: float  # MAPE + λ×|bias_pct|; primary sort key for best-model selection
    training_time_sec: float
    yhat: list[float]
    wape: float = 0.0     # robust % error for low-count/daily series (MAPE explodes there)
    feature_importance: list[dict[str, float]] | None = None


def _mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    safe = np.where(y_true == 0, 1, y_true)
    return float(np.mean(np.abs((safe - y_pred) / safe)) * 100.0)


def _smape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    denom = np.abs(y_true) + np.abs(y_pred)
    denom[denom == 0] = 1
    return float(np.mean(2.0 * np.abs(y_pred - y_true) / denom) * 100.0)


def _wape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Weighted Absolute Percentage Error: sum(|y-ŷ|)/sum(|y|)*100.

    Scale-free like MAPE but robust to low/zero counts (single global denominator),
    so it doesn't explode on sparse daily sales the way MAPE does (95-158% observed).
    """
    denom = float(np.sum(np.abs(y_true)))
    if denom == 0:
        return 0.0
    return float(np.sum(np.abs(y_true - y_pred)) / denom * 100.0)


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[float, float, float, float]:
    mae = float(mean_absolute_error(y_true, y_pred))
    rmse = float(math.sqrt(mean_squared_error(y_true, y_pred)))
    mape = _mape(y_true, y_pred)
    smape = _smape(y_true, y_pred)
    return mae, rmse, mape, smape


# In-memory + disk-backed cache for feature importance.
# Written on every cache update; loaded from disk on first access or startup.
_FEATURE_IMPORTANCE_CACHE: dict[str, dict[str, Any]] = {}
_FI_CACHE_LOADED: bool = False


def _fi_cache_path() -> str:
    return os.path.join(settings.DATA_LANDING_DIR, "_feature_importance_cache.json")


def _load_feature_importance_cache() -> None:
    """Load the persisted cache from disk into memory (idempotent)."""
    global _FEATURE_IMPORTANCE_CACHE, _FI_CACHE_LOADED
    if _FI_CACHE_LOADED:
        return
    path = _fi_cache_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _FEATURE_IMPORTANCE_CACHE = json.load(fh)
            logger.info("Feature importance cache loaded from %s (%d entries)", path, len(_FEATURE_IMPORTANCE_CACHE))
        except Exception as exc:
            logger.warning("Could not load feature importance cache from %s: %s", path, exc)
            _FEATURE_IMPORTANCE_CACHE = {}
    _FI_CACHE_LOADED = True


def _save_feature_importance_cache() -> None:
    """Persist the in-memory cache to disk."""
    path = _fi_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_FEATURE_IMPORTANCE_CACHE, fh)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("Could not persist feature importance cache to %s: %s", path, exc)


def _cache_feature_importance(session_id: str, model_name: str, importance: list[dict[str, float]]) -> None:
    """Store feature importance in memory and persist to disk."""
    _load_feature_importance_cache()
    key = f"{session_id}:{model_name}"
    _FEATURE_IMPORTANCE_CACHE[key] = {
        "model": model_name,
        "factors": importance,
        "cached_at": time.time(),
    }
    _save_feature_importance_cache()


def _get_cached_importance(session_id: str, model_name: str) -> list[dict[str, float]] | None:
    """Retrieve cached feature importance, loading from disk if needed."""
    _load_feature_importance_cache()
    key = f"{session_id}:{model_name}"
    return _FEATURE_IMPORTANCE_CACHE.get(key, {}).get("factors")


def _extract_xgboost_importance(model: XGBRegressor, feature_names: list[str]) -> list[dict[str, float]]:
    """Extract feature importance from XGBoost model."""
    importances = model.feature_importances_
    total = float(np.sum(importances)) if np.sum(importances) > 0 else 1.0
    
    items = []
    for fname, imp in zip(feature_names, importances):
        if imp > 0:
            normalized = float((imp / total) * 100.0)
            items.append({"feature": fname, "importance": float(imp), "normalized_importance": normalized})
    
    items.sort(key=lambda x: x["importance"], reverse=True)
    return items[:10]


def _extract_linear_importance(model: LinearRegression, feature_names: list[str]) -> list[dict[str, float]]:
    """Extract feature importance from Linear Regression (absolute coefficient magnitude)."""
    coefficients = np.abs(model.coef_)
    total = float(np.sum(coefficients)) if np.sum(coefficients) > 0 else 1.0
    
    items = []
    for fname, coef in zip(feature_names, coefficients):
        if coef > 0:
            normalized = float((coef / total) * 100.0)
            items.append({"feature": fname, "importance": float(coef), "normalized_importance": normalized})
    
    items.sort(key=lambda x: x["importance"], reverse=True)
    return items[:10]


def _extract_prophet_importance() -> list[dict[str, float]]:
    """Extract component importance from Prophet model (heuristic)."""
    return [
        {"feature": "trend", "importance": 50.0, "normalized_importance": 50.0},
        {"feature": "seasonality", "importance": 50.0, "normalized_importance": 50.0},
    ]


def _extract_importance_from_model(
    model_name: str,
    trained_model: Any,
    feature_names: list[str] | None = None,
) -> list[dict[str, float]]:
    """Extract feature importance from any trained model."""
    try:
        if model_name == "xgboost" and hasattr(trained_model, 'feature_importances_'):
            return _extract_xgboost_importance(trained_model, feature_names or [])
        
        if model_name == "linear_regression" and hasattr(trained_model, 'coef_'):
            return _extract_linear_importance(trained_model, feature_names or [])
        
        if model_name == "prophet":
            return _extract_prophet_importance()
        
        if model_name in {"seasonal_naive", "sarima", "exp_smoothing"}:
            return [
                {"feature": "seasonality", "importance": 60.0, "normalized_importance": 60.0},
                {"feature": "trend", "importance": 40.0, "normalized_importance": 40.0},
            ]
        
        if model_name == "naive_last":
            return [
                {"feature": "recent_history", "importance": 100.0, "normalized_importance": 100.0},
            ]
        
        if model_name in {"lstm", "chronos", "timesfm", "patchtst", "autogluon"}:
            return [
                {"feature": "temporal_patterns", "importance": 70.0, "normalized_importance": 70.0},
                {"feature": "learned_representations", "importance": 30.0, "normalized_importance": 30.0},
            ]
        
        return []
    except Exception as exc:
        logger.warning(f"Failed to extract importance from {model_name}: {exc}")
        return []


def _segment_clause(target_level: str, target_value: str | None) -> tuple[str, dict[str, Any]]:
    level = (target_level or "service").lower().strip()
    if level == "service":
        return "", {}
    if not target_value:
        raise ValueError(f"target_value is required for target_level={level}")
    normalized_value = target_value.strip()
    if level == "product":
        return " WHERE LOWER(TRIM(product_id)) = LOWER(TRIM(:target_value))", {"target_value": normalized_value}
    if level == "category":
        return " WHERE LOWER(TRIM(product_category)) = LOWER(TRIM(:target_value))", {"target_value": normalized_value}
    if level == "region":
        return " WHERE LOWER(TRIM(region_key)) = LOWER(TRIM(:target_value))", {"target_value": normalized_value}
    raise ValueError(f"Unsupported target_level: {target_level}")


def _offer_based_source_query(granularity: str) -> str:
    """Build a sales source query using fact_ventes + dim_offres for product/category targets."""
    if granularity == "monthly":
        date_expr = "date_trunc('month', t.date::timestamp)::date"
        order_expr = "date ASC"
    else:
        date_expr = "t.date"
        order_expr = "date ASC"

    return f"""
        SELECT
            {date_expr} AS date,
            s.service_code,
            COALESCE(o.offre_code, 'UNKNOWN') AS product_id,
            COALESCE(o.offre_name, o.offre_code, 'UNKNOWN') AS product_name,
            COALESCE(NULLIF(TRIM(o.category), ''), NULLIF(TRIM(o.debit), ''), 'UNKNOWN') AS product_category,
            COALESCE(g.region_code, g.governorate, g.city, 'UNKNOWN') AS region_key,
            COALESCE(g.governorate, g.city, 'UNKNOWN') AS region_label,
            COUNT(*) AS nb_ventes,
            COUNT(DISTINCT v.dealer_id) AS nb_dealers_actifs,
            COUNT(DISTINCT CASE WHEN v.promo_id IS NOT NULL THEN v.vente_id ELSE NULL END) AS nb_ventes_promo,
            SUM(CASE WHEN v.promo_id IS NOT NULL THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0)::numeric * 100 AS pct_ventes_promo,
            AVG(COALESCE(o.price, 0)) AS prix_moyen
        FROM mart.fact_ventes v
        JOIN mart.dim_temps t ON v.date_id = t.date_id
        JOIN mart.dim_services s ON v.service_id = s.service_id
        LEFT JOIN mart.dim_geographie g ON v.geo_id = g.geo_id
        LEFT JOIN mart.dim_offres o ON v.offre_id = o.offre_id
        GROUP BY
            {date_expr},
            s.service_code,
            COALESCE(o.offre_code, 'UNKNOWN'),
            COALESCE(o.offre_name, o.offre_code, 'UNKNOWN'),
            COALESCE(NULLIF(TRIM(o.category), ''), NULLIF(TRIM(o.debit), ''), 'UNKNOWN'),
            COALESCE(g.region_code, g.governorate, g.city, 'UNKNOWN'),
            COALESCE(g.governorate, g.city, 'UNKNOWN')
        ORDER BY {order_expr}
    """


def _fill_temporal_gaps(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if df.empty:
        return df

    work = df.copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work.sort_values("date")

    # If there are duplicate rows for the same date, aggregate them
    if work["date"].duplicated().any():
        numeric_cols = work.select_dtypes(include=["number"]).columns.tolist()
        other_cols = [c for c in work.columns if c not in numeric_cols and c != "date"]
        agg_dict: dict[str, str] = {c: "sum" for c in numeric_cols}
        agg_dict.update({c: "first" for c in other_cols})
        work = work.groupby("date", as_index=False).agg(agg_dict).sort_values("date")

    full_index = pd.date_range(start=work["date"].min(), end=work["date"].max(), freq=freq)
    work = work.set_index("date").reindex(full_index)
    work.index.name = "date"

    numeric_defaults = {
        "nb_ventes": 0.0,
        "nb_dealers_actifs": 0.0,
        "nb_ventes_promo": 0.0,
        "pct_ventes_promo": 0.0,
        "prix_moyen": 0.0,
    }
    for column, default in numeric_defaults.items():
        if column in work.columns:
            work[column] = work[column].fillna(default)

    for column in ["service_code", "product_id", "product_name", "product_category", "region_key", "region_label"]:
        if column in work.columns:
            work[column] = work[column].ffill().bfill().fillna("UNKNOWN")

    if "nb_ventes_promo" in work.columns and "nb_ventes" in work.columns:
        ratio = np.divide(
            work["nb_ventes_promo"].to_numpy(dtype=float),
            np.where(work["nb_ventes"].to_numpy(dtype=float) == 0, 1.0, work["nb_ventes"].to_numpy(dtype=float)),
        )
        work["pct_ventes_promo"] = np.where(work["nb_ventes"].to_numpy(dtype=float) > 0, ratio * 100.0, 0.0)

    if "prix_moyen" in work.columns:
        work["prix_moyen"] = work["prix_moyen"].interpolate(limit_direction="both").fillna(0.0)

    return work.reset_index()


def load_monthly_sales(
    db: Session,
    service_code: str | None = None,
    target_level: str = "service",
    target_value: str | None = None,
) -> pd.DataFrame:
    clause, params = _segment_clause(target_level, target_value)
    level = (target_level or "service").lower().strip()
    order_column = "month_start"
    if level in {"product", "category"}:
        order_column = "date"
        query = f"""
            WITH source AS (
                {_offer_based_source_query("monthly")}
            )
            SELECT * FROM source
        """
    else:
        query = """
            SELECT
                month_start AS date,
                service_code,
                product_id,
                product_name,
                product_category,
                region_key,
                region_label,
                nb_ventes,
                nb_dealers_actifs,
                nb_ventes_promo,
                pct_ventes_promo,
                prix_moyen
            FROM mart.vw_monthly_sales_forecasting
        """
    filters: list[str] = []
    if service_code:
        filters.append("service_code = :service_code")
        params["service_code"] = service_code
    if clause:
        filters.append(clause.replace(" WHERE ", "", 1))
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += f" ORDER BY {order_column} ASC"

    rows = db.execute(text(query), params).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "service_code",
                "product_id",
                "product_name",
                "product_category",
                "region_key",
                "region_label",
                "nb_ventes",
                "nb_dealers_actifs",
                "nb_ventes_promo",
                "pct_ventes_promo",
                "prix_moyen",
            ]
        )

    raw = pd.DataFrame(
        rows,
        columns=[
            "date",
            "service_code",
            "product_id",
            "product_name",
            "product_category",
            "region_key",
            "region_label",
            "nb_ventes",
            "nb_dealers_actifs",
            "nb_ventes_promo",
            "pct_ventes_promo",
            "prix_moyen",
        ],
    )
    aggregated = (
        raw.groupby("date", as_index=False)
        .agg(
            {
                "service_code": "first",
                "product_id": "first",
                "product_name": "first",
                "product_category": "first",
                "region_key": "first",
                "region_label": "first",
                "nb_ventes": "sum",
                "nb_dealers_actifs": "mean",
                "nb_ventes_promo": "sum",
                "pct_ventes_promo": "mean",
                "prix_moyen": "mean",
            }
        )
        .sort_values("date")
    )
    return _fill_temporal_gaps(aggregated, MONTHLY_FREQ)


def load_daily_sales(
    db: Session,
    service_code: str | None = None,
    target_level: str = "service",
    target_value: str | None = None,
) -> pd.DataFrame:
    clause, params = _segment_clause(target_level, target_value)
    level = (target_level or "service").lower().strip()
    if level in {"product", "category"}:
        query = f"""
            WITH source AS (
                {_offer_based_source_query("daily")}
            )
            SELECT * FROM source
        """
    else:
        query = """
            SELECT
                date,
                service_code,
                product_id,
                product_name,
                product_category,
                region_key,
                region_label,
                nb_ventes,
                nb_dealers_actifs,
                nb_ventes_promo,
                pct_ventes_promo,
                prix_moyen
            FROM mart.vw_daily_sales_forecasting
        """
    filters: list[str] = []
    if service_code:
        filters.append("service_code = :service_code")
        params["service_code"] = service_code
    if clause:
        filters.append(clause.replace(" WHERE ", "", 1))
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY date ASC"

    rows = db.execute(text(query), params).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "service_code",
                "product_id",
                "product_name",
                "product_category",
                "region_key",
                "region_label",
                "nb_ventes",
                "nb_dealers_actifs",
                "nb_ventes_promo",
                "pct_ventes_promo",
                "prix_moyen",
            ]
        )

    raw = pd.DataFrame(
        rows,
        columns=[
            "date",
            "service_code",
            "product_id",
            "product_name",
            "product_category",
            "region_key",
            "region_label",
            "nb_ventes",
            "nb_dealers_actifs",
            "nb_ventes_promo",
            "pct_ventes_promo",
            "prix_moyen",
        ],
    )
    aggregated = (
        raw.groupby("date", as_index=False)
        .agg(
            {
                "service_code": "first",
                "product_id": "first",
                "product_name": "first",
                "product_category": "first",
                "region_key": "first",
                "region_label": "first",
                "nb_ventes": "sum",
                "nb_dealers_actifs": "mean",
                "nb_ventes_promo": "sum",
                "pct_ventes_promo": "mean",
                "prix_moyen": "mean",
            }
        )
        .sort_values("date")
    )
    return _fill_temporal_gaps(aggregated, DAILY_FREQ)


def list_available_services(db: Session) -> list[str]:
    """Return the distinct service codes that actually have sales data.

    This is the source of truth for "which services can I forecast" — derived from
    the data (fact_ventes via the forecasting views), NOT from any upload session.
    Lets every service be selected/forecast equally instead of being gated by the
    single service an upload session happened to detect.
    """
    try:
        rows = db.execute(
            text(
                "SELECT DISTINCT service_code FROM mart.vw_monthly_sales_forecasting "
                "WHERE service_code IS NOT NULL AND TRIM(service_code) <> '' "
                "ORDER BY service_code"
            )
        ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception as exc:
        logger.warning("list_available_services failed: %s", exc)
        return []


def resolve_service_code(
    db: Session,
    request_service_type: str | None,
    session_service_detected: str | None,
) -> str | None:
    """Resolve which service to forecast, treating all services equally.

    Priority:
      1. Explicit request_service_type (validated against the data) — 'ALL'/'*' → None.
      2. The session's detected service, but only if it's a real service present in the
         data (a multi-service or stock upload detects 'UNKNOWN', which must NOT be used
         as a filter — it would match nothing).
      3. None → aggregate across all services.
    Raises ValueError for an explicit service that isn't in the data.
    """
    available = list_available_services(db)
    if request_service_type and request_service_type.strip():
        st = request_service_type.strip().upper()
        if st in {"ALL", "*"}:
            return None
        if available and st not in available:
            raise ValueError(
                f"service_type '{request_service_type}' has no sales data. "
                f"Available services: {available} (or 'ALL')."
            )
        return st
    if session_service_detected and session_service_detected.strip().upper() not in {"", "UNKNOWN"}:
        sd = session_service_detected.strip().upper()
        if not available or sd in available:
            return sd
    return None


def list_target_values(
    db: Session,
    granularity: str,
    target_level: str,
    service_code: str | None = None,
) -> list[str]:
    level = (target_level or "service").lower().strip()
    if level == "service":
        # Option C: the service list comes from the data, not a session.
        return list_available_services(db)

    if level not in {"region", "category", "product"}:
        raise ValueError(f"Unsupported target_level: {target_level}")

    if level in {"product", "category"}:
        query = """
            SELECT DISTINCT
                CASE
                    WHEN :level = 'product' THEN COALESCE(o.offre_code, 'UNKNOWN')
                    ELSE COALESCE(NULLIF(TRIM(o.category), ''), NULLIF(TRIM(o.debit), ''), 'UNKNOWN')
                END AS value
            FROM mart.fact_ventes v
            JOIN mart.dim_services s ON v.service_id = s.service_id
            LEFT JOIN mart.dim_offres o ON v.offre_id = o.offre_id
            WHERE 1=1
        """
        params: dict[str, Any] = {"level": level}
        if service_code:
            query += " AND s.service_code = :service_code"
            params["service_code"] = service_code
        query += " ORDER BY value"

        rows = db.execute(text(query), params).fetchall()
        values = [str(row[0]) for row in rows if row and row[0] is not None and str(row[0]).strip()]
        cleaned = [v for v in values if v.upper() != "UNKNOWN"]
        return cleaned or values

    source_view = "mart.vw_daily_sales_forecasting" if granularity == "daily" else "mart.vw_monthly_sales_forecasting"
    column_map = {
        "region": "region_key",
        "category": "product_category",
        "product": "product_id",
    }
    target_column = column_map[level]

    query = f"""
        SELECT DISTINCT {target_column} AS value
        FROM {source_view}
        WHERE {target_column} IS NOT NULL
          AND TRIM(CAST({target_column} AS TEXT)) <> ''
    """
    params: dict[str, Any] = {}
    if service_code:
        query += " AND service_code = :service_code"
        params["service_code"] = service_code
    query += " ORDER BY value"

    rows = db.execute(text(query), params).fetchall()
    return [str(row[0]) for row in rows if row and row[0] is not None]


def _normalize_lookup_token(value: str) -> str:
    token = unicodedata.normalize("NFKD", value or "")
    token = "".join(ch for ch in token if not unicodedata.combining(ch))
    token = token.lower().replace("_", " ").replace("-", " ")
    return " ".join(token.split())


def resolve_target_value(
    db: Session,
    granularity: str,
    target_level: str,
    target_value: str | None,
    service_code: str | None = None,
) -> str | None:
    """Resolve a target value to a known member, with typo tolerance for UI input."""
    level = (target_level or "service").lower().strip()
    if level == "service":
        return None

    raw_value = (target_value or "").strip()
    if not raw_value:
        raise ValueError(f"target_value is required for target_level={level}")

    available = list_target_values(
        db=db,
        granularity=granularity,
        target_level=level,
        service_code=service_code,
    )
    if not available:
        raise ValueError(f"No sales data found for target_level={target_level} and target_value={target_value}")

    requested_token = _normalize_lookup_token(raw_value)
    by_token: dict[str, str] = {_normalize_lookup_token(item): item for item in available}

    if requested_token in by_token:
        return by_token[requested_token]

    close = difflib.get_close_matches(requested_token, list(by_token.keys()), n=1, cutoff=0.75)
    if close:
        resolved = by_token[close[0]]
        logger.info(
            "Resolved target_value '%s' to '%s' for level=%s granularity=%s",
            raw_value,
            resolved,
            level,
            granularity,
        )
        return resolved

    preview = ", ".join(available[:5])
    raise ValueError(
        f"No sales data found for target_level={target_level} and target_value={target_value}. "
        f"Valid examples: {preview}"
    )


def _build_features(
    df: pd.DataFrame,
    granularity: str = "daily",
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
) -> tuple[pd.DataFrame, pd.Series]:
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work.sort_values("date").reset_index(drop=True)
    work["trend_index"] = np.arange(len(work), dtype=float)
    work["month"] = work["date"].dt.month
    work["quarter"] = work["date"].dt.quarter
    work["year"] = work["date"].dt.year
    work["promo_active"] = (work["nb_ventes_promo"] > 0).astype(int)
    work["promo_rate"] = np.where(work["nb_ventes"] > 0, work["nb_ventes_promo"] / work["nb_ventes"], 0.0)

    encoder = OneHotEncoder(sparse_output=False, handle_unknown="ignore")
    service_oh = encoder.fit_transform(work[["service_code"]])
    service_cols = [f"service_{name}" for name in encoder.categories_[0]]
    service_df = pd.DataFrame(service_oh, columns=service_cols, index=work.index)

    if granularity == "monthly":
        work["month_sin"] = np.sin(2.0 * np.pi * work["month"] / 12.0)
        work["month_cos"] = np.cos(2.0 * np.pi * work["month"] / 12.0)
        for lag in [1, 2, 3, 12]:
            work[f"sales_lag_{lag}"] = work["nb_ventes"].shift(lag)
        for window in [3, 6, 12]:
            work[f"sales_roll_{window}"] = work["nb_ventes"].rolling(window, min_periods=1).mean()
            work[f"price_roll_{window}"] = work["prix_moyen"].rolling(window, min_periods=1).mean()

        work = work.ffill().fillna(0.0)
        feature_cols: list[str] = ["trend_index"]
        if include_calendar:
            feature_cols += ["month", "quarter", "month_sin", "month_cos"]
        if include_promotions:
            feature_cols += ["promo_active", "promo_rate"]
        if include_price:
            feature_cols += ["prix_moyen"]
        feature_cols += ["nb_dealers_actifs",
                         "sales_lag_1", "sales_lag_2", "sales_lag_3", "sales_lag_12",
                         "sales_roll_3", "sales_roll_6", "sales_roll_12"]
        if include_price:
            feature_cols += ["price_roll_3", "price_roll_6", "price_roll_12"]
    else:
        work["day_of_week"] = work["date"].dt.weekday
        work["is_weekend"] = (work["day_of_week"] >= 5).astype(int)
        work["is_ramadan"] = _make_ramadan_mask(work["date"])
        work["is_holiday"] = _make_holiday_mask(work["date"])
        work["month_sin"] = np.sin(2.0 * np.pi * work["month"] / 12.0)
        work["month_cos"] = np.cos(2.0 * np.pi * work["month"] / 12.0)
        work["prix_moyen_lag7"] = work["prix_moyen"].rolling(7, min_periods=1).mean()
        work["nb_dealers_actifs_lag7"] = work["nb_dealers_actifs"].rolling(7, min_periods=1).mean()
        work["nb_dealers_actifs_lag30"] = work["nb_dealers_actifs"].rolling(30, min_periods=1).mean()
        work = work.ffill().fillna(0.0)
        feature_cols = ["trend_index"]
        if include_calendar:
            feature_cols += ["day_of_week", "month", "quarter", "month_sin", "month_cos",
                             "is_weekend", "is_ramadan", "is_holiday"]
        if include_promotions:
            feature_cols += ["promo_active", "promo_rate"]
        if include_price:
            feature_cols += ["prix_moyen_lag7"]
        feature_cols += ["nb_dealers_actifs_lag7", "nb_dealers_actifs_lag30"]

    x = pd.concat([work[feature_cols], service_df], axis=1)
    y = work["nb_ventes"].astype(float)
    return x, y


def _run_linear_regression(x_train: pd.DataFrame, y_train: pd.Series, x_test: pd.DataFrame) -> np.ndarray:
    model = LinearRegression()
    model.fit(x_train, y_train)
    return model.predict(x_test)


def _run_naive_last(y_train: pd.Series, steps: int) -> np.ndarray:
    if y_train.empty:
        raise ValueError("No training history available for naive_last")
    return np.repeat(float(y_train.iloc[-1]), steps)


def _run_seasonal_naive(y_train: pd.Series, steps: int, season_length: int) -> np.ndarray:
    if y_train.empty:
        raise ValueError("No training history available for seasonal_naive")

    season_length = max(1, int(season_length))
    if len(y_train) < season_length:
        raise ValueError(
            f"Insufficient history for seasonal_naive: need at least {season_length} points, got {len(y_train)}"
        )

    season = y_train.iloc[-season_length:].to_numpy(dtype=float)
    repeats = int(math.ceil(steps / season_length))
    forecast = np.tile(season, repeats)[:steps]
    return np.asarray(forecast, dtype=float)


def _run_xgboost(x_train: pd.DataFrame, y_train: pd.Series, x_test: pd.DataFrame) -> np.ndarray:
    model = XGBRegressor(
        n_estimators=250,
        learning_rate=0.05,
        max_depth=4,
        subsample=0.9,
        colsample_bytree=0.9,
        random_state=42,
        objective="reg:squarederror",
    )
    model.fit(x_train, y_train)
    return model.predict(x_test)


def _run_feature_model_autoregressive(
    model_name: str,
    history: pd.DataFrame,
    steps: int,
    granularity: str,
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
) -> np.ndarray:
    """
    Recursive multi-step inference for feature-based regressors.
    Future feature rows are synthesized from recent history and prior predictions.
    """
    if steps <= 0:
        return np.asarray([], dtype=float)

    _feat_kwargs = dict(
        granularity=granularity,
        include_promotions=include_promotions,
        include_price=include_price,
        include_calendar=include_calendar,
    )

    simulated = history.copy().sort_values("date").reset_index(drop=True)
    predictions: list[float] = []

    for _ in range(steps):
        x_train, y_train = _build_features(simulated, **_feat_kwargs)

        next_date = pd.to_datetime(simulated["date"].max()) + (
            pd.offsets.MonthBegin(1) if granularity == "monthly" else timedelta(days=1)
        )

        next_row = simulated.iloc[-1].copy()
        next_row["date"] = next_date
        next_row["nb_ventes"] = np.nan

        if "nb_dealers_actifs" in simulated.columns:
            next_row["nb_dealers_actifs"] = float(simulated["nb_dealers_actifs"].tail(min(3, len(simulated))).mean())
        if "prix_moyen" in simulated.columns:
            next_row["prix_moyen"] = float(simulated["prix_moyen"].tail(min(3, len(simulated))).mean())
        if "pct_ventes_promo" in simulated.columns:
            next_row["pct_ventes_promo"] = float(simulated["pct_ventes_promo"].tail(min(3, len(simulated))).mean())
        if "nb_ventes_promo" in simulated.columns:
            next_row["nb_ventes_promo"] = float(simulated["nb_ventes_promo"].tail(min(3, len(simulated))).mean())

        candidate = pd.concat([simulated, pd.DataFrame([next_row])], ignore_index=True)
        x_future, _ = _build_features(candidate, **_feat_kwargs)
        x_next = x_future.iloc[[-1]]

        if model_name == "linear_regression":
            y_next = float(_run_linear_regression(x_train, y_train, x_next)[0])
        elif model_name == "xgboost":
            y_next = float(_run_xgboost(x_train, y_train, x_next)[0])
        else:
            raise ValueError(f"Unsupported feature model: {model_name}")

        y_next = max(0.0, y_next)
        predictions.append(y_next)

        realized = next_row.copy()
        realized["nb_ventes"] = y_next
        promo_pct = float(simulated["pct_ventes_promo"].tail(min(3, len(simulated))).mean()) if "pct_ventes_promo" in simulated.columns else 0.0
        realized["pct_ventes_promo"] = promo_pct
        realized["nb_ventes_promo"] = max(0.0, y_next * promo_pct / 100.0)
        simulated = pd.concat([simulated, pd.DataFrame([realized])], ignore_index=True)

    return np.asarray(predictions, dtype=float)


def _run_forecast_for_model(
    model_key: str,
    y_train: pd.Series,
    history: pd.DataFrame,
    horizon: int,
    granularity: str,
    freq: str,
    seasonal_period: int,
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
) -> np.ndarray:
    """Run forecast for one supported model using its dedicated inference path."""
    _ar_kwargs = dict(
        include_promotions=include_promotions,
        include_price=include_price,
        include_calendar=include_calendar,
    )
    if model_key == "sarima":
        return _run_sarima(y_train, horizon, seasonal_period)
    if model_key == "linear_regression":
        return _run_feature_model_autoregressive(model_key, history, horizon, granularity, **_ar_kwargs)
    if model_key == "xgboost":
        return _run_feature_model_autoregressive(model_key, history, horizon, granularity, **_ar_kwargs)
    if model_key == "exp_smoothing":
        return _run_exp_smoothing(y_train, horizon)
    if model_key == "prophet":
        dates = history["date"] if history is not None and "date" in history.columns else None
        return _run_prophet(y_train, horizon, freq=freq, dates=dates)
    if model_key == "lstm":
        return _run_lstm(y_train, horizon)
    if model_key == "naive_last":
        return _run_naive_last(y_train, horizon)
    if model_key == "seasonal_naive":
        return _run_seasonal_naive(y_train, horizon, seasonal_period)
    if model_key in GENERATIVE_MODELS:
        return _run_generative_model(model_key, y_train, horizon, freq)
    raise ValueError(f"Unsupported model: {model_key}. Supported models: {', '.join(ALL_MODELS)}")


def _run_sarima(y_train: pd.Series, steps: int, seasonal_period: int = DAILY_SEASONAL_PERIOD) -> np.ndarray:
    if len(y_train) < max(8, seasonal_period):
        raise ValueError(
            f"Insufficient history for sarima: need at least {max(8, seasonal_period)} points, got {len(y_train)}"
        )
    model = SARIMAX(y_train, order=(1, 1, 1), seasonal_order=(1, 1, 1, seasonal_period), enforce_stationarity=False)
    fit = model.fit(disp=False)
    return np.asarray(fit.forecast(steps=steps))


def _run_sarima_with_ci(
    y_train: pd.Series,
    steps: int,
    seasonal_period: int = DAILY_SEASONAL_PERIOD,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SARIMA point forecast + native 80 % confidence intervals from the fitted model."""
    if len(y_train) < max(8, seasonal_period):
        raise ValueError(
            f"Insufficient history for sarima: need at least {max(8, seasonal_period)} points, got {len(y_train)}"
        )
    model = SARIMAX(y_train, order=(1, 1, 1), seasonal_order=(1, 1, 1, seasonal_period), enforce_stationarity=False)
    fit = model.fit(disp=False)
    fc = fit.get_forecast(steps=steps)
    ci = fc.conf_int(alpha=0.20)  # alpha=0.20 → 80 % CI
    yhat = np.asarray(fc.predicted_mean, dtype=float)
    lower = np.asarray(ci.iloc[:, 0], dtype=float)
    upper = np.asarray(ci.iloc[:, 1], dtype=float)
    return yhat, lower, upper


def _run_exp_smoothing(y_train: pd.Series, steps: int) -> np.ndarray:
    model = ExponentialSmoothing(y_train, trend="add", seasonal=None)
    fit = model.fit(optimized=True)
    return np.asarray(fit.forecast(steps=steps))


def _run_prophet(y_train: pd.Series, steps: int, freq: str = DAILY_FREQ, dates: pd.Series | None = None) -> np.ndarray:
    try:
        _ensure_cmdstan_available()

        ds = pd.to_datetime(dates).reset_index(drop=True) if dates is not None else pd.date_range(start="2020-01-01", periods=len(y_train), freq=freq)
        pdf = pd.DataFrame({"ds": ds, "y": y_train.values})
        model = Prophet(
            daily_seasonality=(freq == DAILY_FREQ),
            weekly_seasonality=(freq == DAILY_FREQ),
            yearly_seasonality=True,
            stan_backend="CMDSTANPY",
        )
        model.fit(pdf)
        future = model.make_future_dataframe(periods=steps, freq=freq)
        fcst = model.predict(future)
        return fcst["yhat"].tail(steps).to_numpy()
    except Exception as exc:
        raise RuntimeError(f"Prophet forecasting failed: {exc}") from exc


def _run_prophet_with_ci(
    y_train: pd.Series,
    steps: int,
    freq: str = DAILY_FREQ,
    dates: pd.Series | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Prophet point forecast + native 80 % uncertainty intervals."""
    try:
        _ensure_cmdstan_available()
        ds = pd.to_datetime(dates).reset_index(drop=True) if dates is not None else pd.date_range(start="2020-01-01", periods=len(y_train), freq=freq)
        pdf = pd.DataFrame({"ds": ds, "y": y_train.values})
        model = Prophet(
            daily_seasonality=(freq == DAILY_FREQ),
            weekly_seasonality=(freq == DAILY_FREQ),
            yearly_seasonality=True,
            interval_width=0.80,  # 80 % CI — consistent with the sqrt-band default
            stan_backend="CMDSTANPY",
        )
        model.fit(pdf)
        future = model.make_future_dataframe(periods=steps, freq=freq)
        fcst = model.predict(future).tail(steps)
        yhat = fcst["yhat"].to_numpy(dtype=float)
        lower = fcst["yhat_lower"].to_numpy(dtype=float)
        upper = fcst["yhat_upper"].to_numpy(dtype=float)
        return yhat, lower, upper
    except Exception as exc:
        raise RuntimeError(f"Prophet CI extraction failed: {exc}") from exc


def _ensure_cmdstan_available() -> None:
    global _CMDSTAN_READY
    if _CMDSTAN_READY:
        return

    import cmdstanpy

    def _is_valid_cmdstan(path: str | None) -> bool:
        return bool(path) and os.path.isfile(os.path.join(path, "makefile"))

    # 1. Explicit config path takes priority — no discovery, no network I/O.
    if settings.CMDSTAN_PATH:
        if not _is_valid_cmdstan(settings.CMDSTAN_PATH):
            raise RuntimeError(
                f"CMDSTAN_PATH='{settings.CMDSTAN_PATH}' is set but no valid CmdStan "
                "installation found there (missing 'makefile')."
            )
        cmdstan_path = settings.CMDSTAN_PATH
    else:
        # 2. Try whatever cmdstanpy already knows about.
        try:
            cmdstan_path = cmdstanpy.cmdstan_path()
        except Exception:
            cmdstan_path = None

        # 3. Scan ~/.cmdstan for any pre-built version.
        if not _is_valid_cmdstan(cmdstan_path):
            home_cmdstan = os.path.expanduser("~/.cmdstan")
            if os.path.isdir(home_cmdstan):
                for entry in sorted(os.listdir(home_cmdstan), reverse=True):
                    candidate = os.path.join(home_cmdstan, entry)
                    if os.path.isdir(candidate) and _is_valid_cmdstan(candidate):
                        cmdstan_path = candidate
                        break

        if not _is_valid_cmdstan(cmdstan_path):
            raise RuntimeError(
                "CmdStan not found. Set CMDSTAN_PATH in your environment to a valid "
                "CmdStan directory, or pre-install it with: "
                "python -c \"import cmdstanpy; cmdstanpy.install_cmdstan()\""
            )

    # Link Prophet's bundled path to the resolved installation so both backends agree.
    try:
        import prophet

        bundled_cmdstan = os.path.join(
            os.path.dirname(prophet.__file__),
            "stan_model",
            "cmdstan-2.33.1",
        )
        if not _is_valid_cmdstan(bundled_cmdstan):
            if os.path.islink(bundled_cmdstan):
                os.unlink(bundled_cmdstan)
            elif os.path.isdir(bundled_cmdstan):
                shutil.rmtree(bundled_cmdstan)
            os.symlink(cmdstan_path, bundled_cmdstan)
    except Exception as exc:
        logger.warning("Could not link Prophet bundled CmdStan to %s: %s", cmdstan_path, exc)

    cmdstanpy.set_cmdstan_path(cmdstan_path)
    _CMDSTAN_READY = True


def _run_lstm(y_train: pd.Series, steps: int) -> np.ndarray:
    """Two-layer LSTM with early stopping, trained with PyTorch, recursive multi-step forecast."""
    try:
        import torch
        import torch.nn as nn
    except ImportError as exc:
        raise RuntimeError("PyTorch is required for the LSTM model but is not installed.") from exc

    values = y_train.values.astype(float)
    n = len(values)
    if n < 8:
        raise ValueError(f"Insufficient history for lstm: need at least 8 points, got {n}")

    # Adaptive hyperparams based on series length
    lookback = min(24, max(6, n // 4))
    hidden_size = 64 if n >= 24 else 32
    num_layers = 2 if n >= 24 else 1
    max_epochs = 300
    patience = 20            # early-stopping patience
    val_fraction = 0.15      # fraction of sequences held out for val loss

    mean_v = float(np.mean(values))
    std_v = float(np.std(values)) if np.std(values) > 1e-6 else 1.0
    normed = (values - mean_v) / std_v

    Xs, Ys = [], []
    for i in range(n - lookback):
        Xs.append(normed[i : i + lookback])
        Ys.append(normed[i + lookback])

    X_all = torch.FloatTensor(np.array(Xs)).unsqueeze(-1)  # (N, lookback, 1)
    Y_all = torch.FloatTensor(np.array(Ys))

    # Train / val split (chronological — never shuffle time series)
    n_val = max(1, int(len(Xs) * val_fraction))
    n_tr = len(Xs) - n_val
    X_tr, Y_tr = X_all[:n_tr], Y_all[:n_tr]
    X_val, Y_val = X_all[n_tr:], Y_all[n_tr:]

    class _LSTMNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.lstm = nn.LSTM(1, hidden_size, num_layers=num_layers, batch_first=True, dropout=0.1 if num_layers > 1 else 0.0)
            self.fc = nn.Linear(hidden_size, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            out, _ = self.lstm(x)
            return self.fc(out[:, -1, :]).squeeze(-1)

    net = _LSTMNet()
    opt = torch.optim.Adam(net.parameters(), lr=0.005)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, patience=patience // 2, factor=0.5)
    loss_fn = nn.MSELoss()

    best_val_loss = float("inf")
    best_state: dict = {}
    no_improve = 0

    for epoch in range(max_epochs):
        net.train()
        opt.zero_grad()
        loss = loss_fn(net(X_tr), Y_tr)
        loss.backward()
        opt.step()

        net.eval()
        with torch.no_grad():
            val_loss = float(loss_fn(net(X_val), Y_val).item())
        scheduler.step(val_loss)

        if val_loss < best_val_loss - 1e-6:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                break

    if best_state:
        net.load_state_dict(best_state)

    net.eval()
    buf = list(normed[-lookback:])
    preds: list[float] = []
    with torch.no_grad():
        for _ in range(steps):
            x_in = torch.FloatTensor(buf[-lookback:]).unsqueeze(0).unsqueeze(-1)
            p = float(net(x_in).item())
            preds.append(p)
            buf.append(p)

    result = np.array(preds) * std_v + mean_v
    return np.maximum(result, 0.0)


def _extract_json_array(text_value: str) -> list[float]:
    try:
        parsed = json.loads(text_value)
        if isinstance(parsed, list):
            return [float(v) for v in parsed]
    except Exception:
        raise ValueError("Could not parse a JSON array from model output")

    raise ValueError("Model output was not a JSON array")


def _coerce_forecast_length(values: list[float], steps: int) -> np.ndarray:
    if steps <= 0:
        return np.asarray([], dtype=float)
    if not values:
        raise ValueError("Generative model returned an empty forecast array")

    cleaned = [float(v) for v in values]
    if len(cleaned) == steps:
        return np.asarray(cleaned, dtype=float)
    if len(cleaned) > steps:
        return np.asarray(cleaned[:steps], dtype=float)

    # If the model returned too few steps, attempt a sensible padding strategy:
    # - If at least two values are available, linearly extrapolate the trend.
    # - Otherwise, repeat the last value (fallback).
    remaining = steps - len(cleaned)
    if len(cleaned) >= 2:
        a = cleaned[-2]
        b = cleaned[-1]
        # compute linear step
        step = b - a
        extra = [b + step * (i + 1) for i in range(remaining)]
        padded = cleaned + extra
        return np.asarray(padded, dtype=float)

    # Fallback: repeat last value
    last = cleaned[-1]
    padded = cleaned + [last] * remaining
    return np.asarray(padded, dtype=float)


def _extract_numeric_series(payload: Any, preferred_keys: list[str]) -> list[float]:
    if isinstance(payload, list):
        if all(isinstance(v, (int, float)) for v in payload):
            return [float(v) for v in payload]
        if payload and all(isinstance(v, dict) for v in payload):
            for key in preferred_keys:
                extracted = [row.get(key) for row in payload if isinstance(row, dict) and row.get(key) is not None]
                if extracted:
                    return [float(v) for v in extracted]

    if isinstance(payload, dict):
        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, list) and value:
                if all(isinstance(v, (int, float)) for v in value):
                    return [float(v) for v in value]
                if all(isinstance(v, dict) for v in value):
                    nested = _extract_numeric_series(value, preferred_keys)
                    if nested:
                        return nested

        for wrapper_key in ["data", "results", "forecast"]:
            wrapped = payload.get(wrapper_key)
            if wrapped is not None:
                nested = _extract_numeric_series(wrapped, preferred_keys)
                if nested:
                    return nested

    return []


def _http_json_post_strict(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any] | list[Any]:
    with httpx.Client(timeout=settings.GENERATIVE_HTTP_TIMEOUT) as client:
        response = client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()


def _run_generative_ollama(name: str, y_train: pd.Series, steps: int) -> np.ndarray:
    history = [float(v) for v in y_train.tail(min(120, len(y_train))).tolist()]
    if not history:
        raise ValueError("No history available for generative forecast")

    system_prompt = (
        "You are a strict time-series forecasting model. "
        "Return ONLY a valid JSON array of numeric forecast values with exact requested length. "
        "Do not include markdown, labels, explanations, or extra keys."
    )
    prompt = (
        f"Model name: {name}\n"
        f"Forecast horizon: {steps}\n"
        "Return exactly one JSON array with horizon values.\n"
        f"Recent history values: {history}\n"
        "Output format example: [123.4, 125.0, 126.2]"
    )

    generated = ollama_client.generate_strict(
        prompt=prompt,
        system_prompt=system_prompt,
        model=name,
        temperature=0.1,
        max_tokens=max(200, steps * 12),
    )
    values = _extract_json_array(generated)
    return _coerce_forecast_length(values, steps)


def _run_generative_timegpt(y_train: pd.Series, steps: int, freq: str) -> np.ndarray:
    if not settings.TIMEGPT_API_KEY:
        raise RuntimeError("TIMEGPT_API_KEY is missing. Configure it to enable model 'timegpt'.")
    if not settings.TIMEGPT_API_URL:
        raise RuntimeError("TIMEGPT_API_URL is missing. Configure it to enable model 'timegpt'.")

    history = [float(v) for v in y_train.tail(min(120, len(y_train))).tolist()]
    if not history:
        raise ValueError("No history available for timegpt forecast")

    payload = {
        "model": settings.TIMEGPT_MODEL_NAME,
        "horizon": steps,
        "h": steps,
        "freq": "M" if freq == MONTHLY_FREQ else "D",
        "series": history,
        "target": history,
    }
    headers = {"Authorization": f"Bearer {settings.TIMEGPT_API_KEY}"}

    response_payload = _http_json_post_strict(settings.TIMEGPT_API_URL, payload, headers=headers)
    values = _extract_numeric_series(
        response_payload,
        preferred_keys=["forecast", "yhat", "TimeGPT", "value", "prediction"],
    )
    if not values:
        raise RuntimeError("timegpt response did not contain a numeric forecast series")
    return _coerce_forecast_length(values, steps)


def _run_generative_chronos(y_train: pd.Series, steps: int) -> np.ndarray:
    """Run Amazon Chronos-T5-Tiny via the chronos-forecasting library (CPU)."""
    try:
        import torch
        from chronos import ChronosPipeline  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"chronos-forecasting not available: {exc}") from exc

    pipeline = ChronosPipeline.from_pretrained(
        "amazon/chronos-t5-tiny",
        device_map="cpu",
        torch_dtype=torch.float32,
    )

    context = torch.tensor(
        y_train.tail(512).values.astype(float), dtype=torch.float32
    ).unsqueeze(0)

    forecast_tensor = pipeline.predict(
        context,
        prediction_length=steps,
        num_samples=20,
        temperature=1.0,
        top_k=50,
        top_p=1.0,
    )
    median_fc = np.quantile(forecast_tensor[0].numpy(), 0.5, axis=0)
    return _coerce_forecast_length(median_fc.tolist(), steps)


def _run_generative_moirai(y_train: pd.Series, steps: int, freq: str) -> np.ndarray:
    """Call a running Moirai HTTP service configured via MOIRAI_API_URL."""
    if not settings.MOIRAI_API_URL:
        raise RuntimeError(
            "Moirai is not configured. Set MOIRAI_API_URL in your environment "
            "to the running Moirai HTTP service endpoint."
        )
    freq_map = {MONTHLY_FREQ: "M", DAILY_FREQ: "D"}
    history = [float(v) for v in y_train.tail(512).tolist()]
    payload = {
        "model": settings.MOIRAI_MODEL_NAME,
        "series": history,
        "horizon": steps,
        "freq": freq_map.get(freq, "M"),
    }
    headers: dict[str, str] = {}
    if settings.MOIRAI_API_KEY and settings.MOIRAI_API_KEY != "local":
        headers["Authorization"] = f"Bearer {settings.MOIRAI_API_KEY}"
    response = _http_json_post_strict(f"{settings.MOIRAI_API_URL.rstrip('/')}/forecast", payload, headers=headers)
    values = _extract_numeric_series(response, preferred_keys=["forecast", "mean", "yhat", "prediction"])
    if not values:
        raise RuntimeError("Moirai response did not contain a numeric forecast series")
    return _coerce_forecast_length(values, steps)


def _run_generative_patchtst(y_train: pd.Series, steps: int, freq: str) -> np.ndarray:
    """Run PatchTST through NeuralForecast."""
    try:
        from neuralforecast import NeuralForecast  # type: ignore
        from neuralforecast.models import PatchTST  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"patchtst (neuralforecast) not available: {exc}") from exc

    train_len = min(512, len(y_train))
    if train_len < 8:
        raise ValueError(f"Insufficient history for patchtst: need at least 8 points, got {train_len}")

    ds = pd.date_range(end=pd.Timestamp.now(), periods=train_len, freq=freq)
    train_df = pd.DataFrame(
        {
            "unique_id": ["series_1"] * train_len,
            "ds": ds,
            "y": y_train.tail(train_len).astype(float).values,
        }
    )

    model = PatchTST(h=steps, input_size=min(64, max(8, train_len - 1)), max_steps=50)
    nf = NeuralForecast(models=[model], freq=freq)
    nf.fit(df=train_df)
    pred_df = nf.predict()

    value_cols = [c for c in pred_df.columns if c not in {"unique_id", "ds"}]
    if not value_cols:
        raise RuntimeError("PatchTST prediction did not include forecast columns")
    vals = pred_df[value_cols[0]].astype(float).tolist()
    return _coerce_forecast_length(vals, steps)


def _run_generative_autogluon(y_train: pd.Series, steps: int, freq: str) -> np.ndarray:
    """Attempt to run AutoGluon TimeSeries predictor if available."""
    try:
        from autogluon.timeseries import TimeSeriesPredictor  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"autogluon.timeseries not available: {exc}") from exc

    from autogluon.timeseries import TimeSeriesDataFrame  # type: ignore

    train_len = len(y_train)
    if train_len < 8:
        raise ValueError(f"Insufficient history for autogluon: need at least 8 points, got {train_len}")

    train_df = pd.DataFrame(
        {
            "item_id": ["series_1"] * train_len,
            "timestamp": pd.date_range(end=pd.Timestamp.now(), periods=train_len, freq=freq),
            "target": y_train.astype(float).values,
        }
    )
    ts_df = TimeSeriesDataFrame.from_data_frame(train_df)
    with tempfile.TemporaryDirectory(prefix="autogluon_ts_") as temp_dir:
        predictor = TimeSeriesPredictor(
            prediction_length=steps,
            target="target",
            freq=freq,
            path=temp_dir,
            verbosity=0,
        )
        predictor.fit(train_data=ts_df, presets="fast_training", time_limit=45)
        forecast = predictor.predict(ts_df)

    if "mean" in forecast.columns:
        vals = forecast.loc["series_1"]["mean"].astype(float).tolist()
    else:
        vals = forecast.loc["series_1"].iloc[:, 0].astype(float).tolist()
    return _coerce_forecast_length(vals, steps)


def _run_generative_timesfm(y_train: pd.Series, steps: int, freq: str) -> np.ndarray:
    """Call a running TimesFM HTTP service configured via TIMESFM_API_URL."""
    if not settings.TIMESFM_API_URL:
        raise RuntimeError(
            "TimesFM is not configured. Set TIMESFM_API_URL in your environment "
            "to the running TimesFM HTTP service endpoint."
        )
    freq_str = "monthly" if freq == MONTHLY_FREQ else "daily"
    history = [float(v) for v in y_train.tail(512).tolist()]
    payload = {
        "series": history,
        "horizon": steps,
        "freq": freq_str,
    }
    headers: dict[str, str] = {}
    if settings.TIMESFM_API_KEY:
        headers["Authorization"] = f"Bearer {settings.TIMESFM_API_KEY}"
    response = _http_json_post_strict(f"{settings.TIMESFM_API_URL.rstrip('/')}/forecast", payload, headers=headers)
    values = _extract_numeric_series(response, preferred_keys=["forecast", "mean", "yhat", "prediction"])
    if not values:
        raise RuntimeError("TimesFM response did not contain a numeric forecast series")
    return _coerce_forecast_length(values, steps)


def _run_generative_model(name: str, y_train: pd.Series, steps: int, freq: str) -> np.ndarray:
    model_name = name.lower().strip()
    if model_name == "chronos":
        return _run_generative_chronos(y_train, steps)
    if model_name == "timesfm":
        return _run_generative_timesfm(y_train, steps, freq)
    if model_name == "timegpt":
        return _run_generative_timegpt(y_train, steps, freq)
    if model_name == "moirai":
        return _run_generative_moirai(y_train, steps, freq)
    if model_name == "patchtst":
        return _run_generative_patchtst(y_train, steps, freq)
    if model_name == "autogluon":
        return _run_generative_autogluon(y_train, steps, freq)
    raise ValueError(f"Unsupported generative model: {name}")


def _extract_percent_shift(text_value: str) -> float:
    matches = re.findall(r"(-?\d+(?:\.\d+)?)\s*%", text_value)
    if not matches:
        return 0.0
    return float(matches[0]) / 100.0


def _start_mlflow_run(run_name: str):
    try:
        return mlflow.start_run(run_name=run_name)
    except Exception:
        return nullcontext()


def _resolve_test_size(total_rows: int, horizon: int, granularity: str) -> int:
    if total_rows <= 2:
        return 1
    if granularity == "monthly":
        desired = max(3, min(horizon, settings.FORECAST_HORIZON_MONTHLY_DEFAULT))
    else:
        desired = max(7, min(horizon, settings.FORECAST_HORIZON_DEFAULT))
    return max(1, min(desired, total_rows - 1))


def train_models(
    db: Session,
    horizon: int,
    enable_generative: bool = True,
    service_code: str | None = None,
    selected_models: list[str] | None = None,
    granularity: str = "daily",
    target_level: str = "service",
    target_value: str | None = None,
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    with tracer.start_as_current_span("sales.training") as _train_span:
        _train_span.set_attribute(_KIND, _CHAIN)
        _train_span.set_attribute("sales.granularity", granularity)
        _train_span.set_attribute("sales.horizon", horizon)
        _train_span.set_attribute("sales.target_level", target_level)
        _train_span.set_attribute("sales.target_value", str(target_value) if target_value else "ALL")
        _train_span.set_attribute("sales.service_code", service_code or "ALL")
        _train_span.set_attribute("sales.enable_generative", enable_generative)
        max_horizon = settings.FORECAST_HORIZON_MONTHLY_DEFAULT if granularity == "monthly" else settings.FORECAST_HORIZON_DEFAULT
        if horizon > max_horizon:
            logger.warning("Requested horizon %s exceeds maximum %s for granularity=%s, clamping.", horizon, max_horizon, granularity)
            horizon = max_horizon

        if granularity == "monthly":
            df = load_monthly_sales(db, service_code=service_code, target_level=target_level, target_value=target_value)
        else:
            df = load_daily_sales(db, service_code=service_code, target_level=target_level, target_value=target_value)
        if df.empty:
            if (target_level or "service").lower().strip() != "service":
                raise ValueError(f"No sales data found for target_level={target_level} and target_value={target_value}")
            raise ValueError("No sales data available for training")

        if len(df) < settings.FORECAST_MIN_SAMPLES:
            raise ValueError(f"Insufficient history. Need at least {settings.FORECAST_MIN_SAMPLES} rows")

        freq = MONTHLY_FREQ if granularity == "monthly" else DAILY_FREQ
        series = _fill_temporal_gaps(df, freq)
        x, y = _build_features(
            series,
            granularity=granularity,
            include_promotions=include_promotions,
            include_price=include_price,
            include_calendar=include_calendar,
        )
        test_size = _resolve_test_size(len(x), horizon, granularity)
        x_train, x_test = x.iloc[:-test_size], x.iloc[-test_size:]
        y_train, y_test = y.iloc[:-test_size], y.iloc[-test_size:]

        selected_lower = [_normalize_model_key(m) for m in (selected_models or [])]
        run_all = not selected_lower or "all" in selected_lower
        models_to_run = CLASSIC_MODELS.copy() if run_all else [m for m in CLASSIC_MODELS if m in selected_lower]

        try:
            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
            mlflow.set_experiment(settings.MLFLOW_EXPERIMENT_NAME)
            mlflow_available = True
        except Exception:
            mlflow_available = False

        results: list[ModelRunResult] = []
        feature_names = list(x.columns)  # Store feature names for importance extraction

        # Mean of the full series — used to normalise bias into a percentage.
        # Using the full series (not just test set) gives a stable demand baseline.
        demand_mean = float(max(1.0, y.mean()))

        def record_result(model_name: str, y_pred: np.ndarray, elapsed: float, trained_model: Any = None) -> None:
            y_true = y_test.to_numpy()
            y_hat = y_pred[: len(y_true)]
            mae, rmse, mape, smape = _metrics(y_true, y_hat)
            wape = _wape(y_true, y_hat)

            # Raw bias: mean(predicted − actual). Positive → over-prediction.
            bias = float(np.mean(y_hat - y_true))

            # Scale-free bias: express as % of average demand so models on
            # different products / horizons are comparable.
            bias_pct = round(abs(bias) / demand_mean * 100.0, 3)

            # Composite score used for ranking:
            #   composite = MAPE + λ × |bias_pct|
            # A model with low MAPE but high systematic bias is penalised.
            # λ = 0.5 means a 10 % bias adds 5 pp to the effective MAPE.
            composite_score = round(mape + BIAS_PENALTY_WEIGHT * bias_pct, 4)

            importance = _extract_importance_from_model(model_name, trained_model, feature_names)
            if session_id and importance:
                _cache_feature_importance(session_id, model_name, importance)

            results.append(
                ModelRunResult(
                    model=model_name,
                    mae=mae,
                    rmse=rmse,
                    mape=mape,
                    smape=smape,
                    bias=bias,
                    bias_pct=bias_pct,
                    composite_score=composite_score,
                    training_time_sec=elapsed,
                    yhat=[float(v) for v in y_pred],
                    wape=wape,
                    feature_importance=importance,
                )
            )

            # Per-model evaluation span so Phoenix shows each model's metrics/timing,
            # matching the inventory pipeline's instrumentation.
            with tracer.start_as_current_span("sales.model_train") as _m_span:
                _m_span.set_attribute(_KIND, _CHAIN)
                _m_span.set_attribute("sales.model_name", model_name)
                _m_span.set_attribute("sales.mae", round(mae, 4))
                _m_span.set_attribute("sales.rmse", round(rmse, 4))
                _m_span.set_attribute("sales.mape", round(mape, 4))
                _m_span.set_attribute("sales.smape", round(smape, 4))
                _m_span.set_attribute("sales.wape", round(wape, 4))
                _m_span.set_attribute("sales.bias_pct", bias_pct)
                _m_span.set_attribute("sales.composite_score", composite_score)
                _m_span.set_attribute("sales.training_time_sec", round(elapsed, 3))
            if mlflow_available:
                try:
                    mlflow.log_metric("mae", mae)
                    mlflow.log_metric("rmse", rmse)
                    mlflow.log_metric("mape", mape)
                    mlflow.log_metric("smape", smape)
                    mlflow.log_metric("bias", bias)
                    mlflow.log_metric("bias_pct", bias_pct)
                    mlflow.log_metric("composite_score", composite_score)
                    mlflow.log_param("model", model_name)
                except Exception:
                    pass

        predictor_map: dict[str, Callable[[], tuple[np.ndarray, Any]]] = {}
        
        # Helpers: fit on x_train/y_train (for feature importance), then evaluate
        # autoregressively on the training-window history — identical to generate_forecast.
        _ar_eval_kwargs = dict(
            granularity=granularity,
            include_promotions=include_promotions,
            include_price=include_price,
            include_calendar=include_calendar,
        )

        def make_linear_trainer():
            model = LinearRegression()
            model.fit(x_train, y_train)
            preds = _run_feature_model_autoregressive(
                "linear_regression", series.iloc[:-test_size], len(y_test), **_ar_eval_kwargs
            )
            return preds, model

        def make_xgboost_trainer():
            model = XGBRegressor(
                n_estimators=250,
                learning_rate=0.05,
                max_depth=4,
                subsample=0.9,
                colsample_bytree=0.9,
                random_state=42,
                objective="reg:squarederror",
            )
            model.fit(x_train, y_train)
            preds = _run_feature_model_autoregressive(
                "xgboost", series.iloc[:-test_size], len(y_test), **_ar_eval_kwargs
            )
            return preds, model
        
        predictor_map = {
            "naive_last": lambda: (_run_naive_last(y_train, len(y_test)), None),
            "seasonal_naive": lambda: (
                _run_seasonal_naive(
                    y_train,
                    len(y_test),
                    MONTHLY_SEASONAL_PERIOD if granularity == "monthly" else DAILY_SEASONAL_PERIOD,
                ),
                None,
            ),
            "linear_regression": make_linear_trainer,
            "xgboost": make_xgboost_trainer,
            "sarima": lambda: (
                _run_sarima(
                    y_train,
                    len(y_test),
                    MONTHLY_SEASONAL_PERIOD if granularity == "monthly" else DAILY_SEASONAL_PERIOD,
                ),
                None,
            ),
            "exp_smoothing": lambda: (_run_exp_smoothing(y_train, len(y_test)), None),
            "prophet": lambda: (_run_prophet(y_train, len(y_test), freq=freq, dates=series["date"].iloc[:-test_size]), None),
            "lstm": lambda: (_run_lstm(y_train, len(y_test)), None),
        }

        for model_name in CLASSIC_MODELS:
            if model_name not in models_to_run:
                continue
            start = time.time()
            try:
                with _start_mlflow_run(model_name):
                    result = predictor_map[model_name]()
                    y_pred, trained_model = result if isinstance(result, tuple) else (result, None)
                record_result(model_name, np.asarray(y_pred), time.time() - start, trained_model)
            except Exception as exc:
                logger.warning("Skipping classic model %s after failure: %s", model_name, exc)

        if enable_generative:
            available_generative_models = get_available_generative_models()
            if not run_all:
                available_generative_models = [m for m in available_generative_models if m in selected_lower]

            for model_name in available_generative_models:
                start = time.time()
                try:
                    with _start_mlflow_run(model_name):
                        y_pred = _run_generative_model(model_name, y_train, len(y_test), freq)
                    record_result(model_name, np.asarray(y_pred), time.time() - start)
                except Exception as exc:
                    logger.warning("Skipping generative model %s after failure: %s", model_name, exc)

        if not results:
            raise ValueError("No forecasting models could be trained for the selected data")

        # Compute a weighted ensemble over the top-3 individual models.
        # Weight = 1 / composite_score (lower error → higher weight).
        _ENSEMBLE_EXCLUDE = {"naive_last", "seasonal_naive"}
        candidate_results = [r for r in results if r.model not in _ENSEMBLE_EXCLUDE]
        if len(candidate_results) >= 2:
            top_n = sorted(candidate_results, key=lambda r: r.composite_score)[:3]
            raw_weights = [1.0 / max(r.composite_score, 1e-6) for r in top_n]
            total_w = sum(raw_weights)
            weights = [w / total_w for w in raw_weights]
            ens_len = max(len(r.yhat) for r in top_n)
            ens_preds = np.zeros(ens_len, dtype=float)
            for r, w in zip(top_n, weights):
                arr = np.array(r.yhat, dtype=float)
                ens_preds[:len(arr)] += w * arr

            y_true_ens = y_test.to_numpy()
            y_hat_ens = ens_preds[: len(y_true_ens)]
            mae_e, rmse_e, mape_e, smape_e = _metrics(y_true_ens, y_hat_ens)
            wape_e = _wape(y_true_ens, y_hat_ens)
            bias_e = float(np.mean(y_hat_ens - y_true_ens))
            bias_pct_e = round(abs(bias_e) / demand_mean * 100.0, 3)
            composite_e = round(mape_e + BIAS_PENALTY_WEIGHT * bias_pct_e, 4)
            component_names = "+".join(r.model for r in top_n)
            results.append(
                ModelRunResult(
                    model="ensemble",
                    mae=mae_e, rmse=rmse_e, mape=mape_e, smape=smape_e,
                    bias=bias_e, bias_pct=bias_pct_e,
                    composite_score=composite_e,
                    training_time_sec=0.0,
                    yhat=[float(v) for v in ens_preds],
                    wape=wape_e,
                    feature_importance=[
                        {"feature": f"ensemble({component_names})", "importance": 100.0, "normalized_importance": 100.0}
                    ],
                )
            )

        sorted_results = sorted(results, key=lambda item: item.composite_score)
        return_data = [item.__dict__ for item in sorted_results]
        if sorted_results:
            _best = sorted_results[0]
            _train_span.set_attribute("sales.models_completed", len(sorted_results))
            _train_span.set_attribute("sales.best_model", _best.model)
            _train_span.set_attribute("sales.best_wape", round(_best.wape, 4))
            _train_span.set_attribute("sales.best_mape", round(_best.mape, 4))
            _train_span.set_attribute("sales.best_composite_score", _best.composite_score)
        return return_data


def _ci_sigma(
    model_key: str,
    y_train: pd.Series,
    history: pd.DataFrame,
    granularity: str,
    freq: str,
    seasonal_period: int,
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
) -> float:
    """
    Estimate one-step forecast error (σ) for CI band construction.

    Each model family gets an appropriate estimator instead of the raw
    series standard deviation (which overestimates uncertainty because it
    includes trend and seasonality, not just forecast error).

    - naive_last       → std of first-differences (random-walk innovation σ)
    - seasonal_naive   → std of period-s differences (seasonal residual σ)
    - exp_smoothing    → sqrt(in-sample SSE / (n-2))  — fitted residual σ
    - xgboost /
      linear_regression→ RMSE on a 3-step holdout split (actual residuals)
    - lstm / generative→ std of first-differences (fast fallback; avoids
                          re-running expensive models)
    """
    n = len(y_train)
    if n < 2:
        return 1.0

    if model_key == "naive_last":
        diffs = np.diff(y_train.values.astype(float))
        return float(max(1.0, np.std(diffs))) if len(diffs) > 0 else float(max(1.0, y_train.std()))

    if model_key == "seasonal_naive":
        if n > seasonal_period:
            s_diffs = (
                y_train.values[seasonal_period:].astype(float)
                - y_train.values[:-seasonal_period].astype(float)
            )
            return float(max(1.0, np.std(s_diffs)))
        diffs = np.diff(y_train.values.astype(float))
        return float(max(1.0, np.std(diffs))) if len(diffs) > 0 else float(max(1.0, y_train.std()))

    if model_key == "exp_smoothing":
        try:
            model = ExponentialSmoothing(y_train, trend="add", seasonal=None)
            fit = model.fit(optimized=True)
            return float(max(1.0, math.sqrt(fit.sse / max(1, n - 2))))
        except Exception:
            pass

    if model_key in {"xgboost", "linear_regression"} and n >= 5:
        holdout = min(3, max(1, n // 5))
        try:
            y_tr = y_train.iloc[:-holdout]
            y_te = y_train.iloc[-holdout:]
            h_tr = history.iloc[:-holdout] if history is not None and len(history) >= n else history
            preds = _run_forecast_for_model(
                model_key=model_key,
                y_train=y_tr,
                history=h_tr,
                horizon=holdout,
                granularity=granularity,
                freq=freq,
                seasonal_period=seasonal_period,
                include_promotions=include_promotions,
                include_price=include_price,
                include_calendar=include_calendar,
            )
            residuals = (
                y_te.values[: len(preds)].astype(float)
                - preds[: len(y_te)].astype(float)
            )
            return float(max(1.0, math.sqrt(float(np.mean(residuals ** 2)))))
        except Exception:
            pass

    # Fallback for lstm, generative models, and any failure above.
    diffs = np.diff(y_train.values.astype(float))
    return float(max(1.0, np.std(diffs))) if len(diffs) > 0 else float(max(1.0, y_train.std()))


def generate_forecast(
    db: Session,
    best_model_name: str,
    horizon: int,
    service_code: str | None = None,
    granularity: str = "daily",
    target_level: str = "service",
    target_value: str | None = None,
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
) -> dict[str, Any]:
    with tracer.start_as_current_span("sales.forecast") as _fc_span:
        _fc_span.set_attribute(_KIND, _CHAIN)
        _fc_span.set_attribute("sales.model", best_model_name)
        _fc_span.set_attribute("sales.granularity", granularity)
        _fc_span.set_attribute("sales.horizon", horizon)
        _fc_span.set_attribute("sales.target_level", target_level)
        _fc_span.set_attribute("sales.target_value", str(target_value) if target_value else "ALL")
        _fc_span.set_attribute("sales.service_code", service_code or "ALL")
        max_horizon = settings.FORECAST_HORIZON_MONTHLY_DEFAULT if granularity == "monthly" else settings.FORECAST_HORIZON_DEFAULT
        if horizon > max_horizon:
            logger.warning("Requested horizon %s exceeds maximum %s for granularity=%s, clamping.", horizon, max_horizon, granularity)
            horizon = max_horizon

        if granularity == "monthly":
            source = load_monthly_sales(db, service_code=service_code, target_level=target_level, target_value=target_value)
        else:
            source = load_daily_sales(db, service_code=service_code, target_level=target_level, target_value=target_value)
        if source.empty:
            if (target_level or "service").lower().strip() != "service":
                raise ValueError(f"No sales data found for target_level={target_level} and target_value={target_value}")
            raise ValueError("No sales data available")

        freq = MONTHLY_FREQ if granularity == "monthly" else DAILY_FREQ
        history = _fill_temporal_gaps(source, freq)
        history = history.sort_values("date")
        y_train = history["nb_ventes"].astype(float)

        model_key = _normalize_model_key(best_model_name)
        seasonal_period = MONTHLY_SEASONAL_PERIOD if granularity == "monthly" else DAILY_SEASONAL_PERIOD

        # Prophet and SARIMA expose native uncertainty intervals from their fitted distributions.
        # All other models get a growing band: ±1.28σ × √h (80 % Gaussian approximation).
        native_lower: np.ndarray | None = None
        native_upper: np.ndarray | None = None
        ensemble_dispersion: np.ndarray | None = None

        if model_key == "ensemble":
            # Run the five best non-baseline models and average their predictions.
            # Equal-weighted average; individual model errors cancel partially.
            _ENSEMBLE_COMPONENTS = ["xgboost", "prophet", "exp_smoothing", "sarima", "linear_regression"]
            _ar_kwargs = dict(
                granularity=granularity,
                include_promotions=include_promotions,
                include_price=include_price,
                include_calendar=include_calendar,
            )
            ensemble_parts: list[np.ndarray] = []
            for em in _ENSEMBLE_COMPONENTS:
                try:
                    if em == "prophet":
                        fc, lo, up = _run_prophet_with_ci(y_train, horizon, freq=freq, dates=history["date"])
                        ensemble_parts.append(fc)
                    elif em == "sarima":
                        fc, lo, up = _run_sarima_with_ci(y_train, horizon, seasonal_period)
                        ensemble_parts.append(fc)
                    else:
                        fc = _run_forecast_for_model(
                            model_key=em, y_train=y_train, history=history,
                            horizon=horizon, granularity=granularity, freq=freq,
                            seasonal_period=seasonal_period, **_ar_kwargs,
                        )
                        ensemble_parts.append(fc)
                except Exception as _ens_exc:
                    logger.debug("Ensemble component %s skipped: %s", em, _ens_exc)
            if not ensemble_parts:
                raise ValueError("Ensemble failed: all component models produced errors")
            forecast_values = np.mean(ensemble_parts, axis=0)
            ensemble_dispersion = np.std(ensemble_parts, axis=0) if len(ensemble_parts) > 1 else None

        elif model_key == "prophet":
            try:
                forecast_values, native_lower, native_upper = _run_prophet_with_ci(y_train, horizon, freq=freq, dates=history["date"])
            except Exception as _ci_exc:
                logger.warning("Prophet CI extraction failed, using point forecast only: %s", _ci_exc)
                forecast_values = _run_prophet(y_train, horizon, freq=freq, dates=history["date"])
        elif model_key == "sarima":
            try:
                forecast_values, native_lower, native_upper = _run_sarima_with_ci(y_train, horizon, seasonal_period)
            except Exception as _ci_exc:
                logger.warning("SARIMA CI extraction failed, using point forecast only: %s", _ci_exc)
                forecast_values = _run_sarima(y_train, horizon, seasonal_period)
        else:
            forecast_values = _run_forecast_for_model(
                model_key=model_key,
                y_train=y_train,
                history=history,
                horizon=horizon,
                granularity=granularity,
                freq=freq,
                seasonal_period=seasonal_period,
                include_promotions=include_promotions,
                include_price=include_price,
                include_calendar=include_calendar,
            )

        last_hist_date = pd.to_datetime(history["date"].max())
        start_date = last_hist_date + (pd.offsets.MonthBegin(1) if granularity == "monthly" else timedelta(days=1))
        forecast_dates = pd.date_range(start=start_date, periods=horizon, freq=freq)

        hist_tail = history.tail(min(120, len(history)))
        historical = [{"date": d.strftime("%Y-%m-%d"), "value": float(v)} for d, v in zip(hist_tail["date"], hist_tail["nb_ventes"])]

        # Estimate one-step σ specific to the model's error structure.
        sigma = _ci_sigma(
            model_key=model_key,
            y_train=y_train,
            history=history,
            granularity=granularity,
            freq=freq,
            seasonal_period=seasonal_period,
            include_promotions=include_promotions,
            include_price=include_price,
            include_calendar=include_calendar,
        )
        forecast = []
        for h_idx, (date_value, pred) in enumerate(zip(forecast_dates, forecast_values), start=1):
            if native_lower is not None and native_upper is not None:
                # Native model CI — already calibrated to the fitted distribution.
                lower = max(0.0, float(native_lower[h_idx - 1]))
                upper = float(native_upper[h_idx - 1])
            elif ensemble_dispersion is not None:
                # Ensemble CI: growing Gaussian band + inter-model spread at this horizon.
                # Inter-model std captures structural disagreement between components;
                # √h term captures that forecasts further out are less certain.
                band = 1.28 * (float(ensemble_dispersion[h_idx - 1]) + sigma * math.sqrt(h_idx))
                lower = max(0.0, float(pred) - band)
                upper = float(pred) + band
            else:
                # Residual-based growing band for single non-native models.
                # σ here is the model's innovation/residual std, not the raw series std.
                band = 1.28 * sigma * math.sqrt(h_idx)
                lower = max(0.0, float(pred) - band)
                upper = float(pred) + band
            forecast.append(
                {
                    "date": date_value.strftime("%Y-%m-%d"),
                    "value": float(pred),
                    "lower_bound": round(lower, 4),
                    "upper_bound": round(upper, 4),
                }
            )

        last_hist = historical[-1]["value"] if historical else 1.0
        last_fc = forecast[-1]["value"] if forecast else last_hist
        change_pct = ((last_fc - last_hist) / max(1.0, last_hist)) * 100.0

        return {
            "historical": historical,
            "forecast": forecast,
            "metadata": {
                "model_used": model_key,
                "mape": None,
                "trend": "hausse" if change_pct >= 0 else "baisse",
                "change_pct": round(change_pct, 2),
            },
        }


def what_if_impact(baseline: list[dict[str, Any]], scenario_text: str) -> tuple[list[dict[str, Any]], float]:
    shift = _extract_percent_shift(scenario_text.lower())
    if shift == 0.0:
        if "promo" in scenario_text.lower():
            shift = 0.20
        elif "rupture" in scenario_text.lower() or "stockout" in scenario_text.lower():
            shift = -0.25

    factor = 1.0 + shift
    scenario = []
    for point in baseline:
        scenario.append({**point, "value": round(float(point["value"]) * factor, 2)})

    return scenario, round(shift * 100.0, 2)


def backtest_score(
    db: Session,
    model_name: str = "linear_regression",
    service_code: str | None = None,
    granularity: str = "daily",
    target_level: str = "service",
    target_value: str | None = None,
    include_promotions: bool = True,
    include_price: bool = True,
    include_calendar: bool = True,
) -> dict[str, Any]:
    """
    Time-series cross-validation for the specified model.

    Returns aggregate metrics (MAE / RMSE / MAPE / SMAPE / bias) plus per-fold
    actuals and predicted series so the frontend can render a visual comparison.
    Bias = mean(predicted − actual); positive → systematic over-prediction.
    """
    if granularity == "monthly":
        df = load_monthly_sales(
            db, service_code=service_code, target_level=target_level, target_value=target_value
        )
        freq = MONTHLY_FREQ
        seasonal_period = MONTHLY_SEASONAL_PERIOD
    else:
        df = load_daily_sales(
            db, service_code=service_code, target_level=target_level, target_value=target_value
        )
        freq = DAILY_FREQ
        seasonal_period = DAILY_SEASONAL_PERIOD

    if len(df) < max(settings.FORECAST_MIN_SAMPLES, 10):
        return {
            "model": model_name,
            "mae": 0.0, "rmse": 0.0, "mape": 0.0, "smape": 0.0, "bias": 0.0,
            "n_folds": 0, "folds": [],
            "warning": "Insufficient data for backtesting.",
        }

    series = _fill_temporal_gaps(df, freq)
    _feat_kwargs = dict(
        granularity=granularity,
        include_promotions=include_promotions,
        include_price=include_price,
        include_calendar=include_calendar,
    )
    x, y = _build_features(series, **_feat_kwargs)
    model_key = _normalize_model_key(model_name)

    # Use 3 folds max to keep response time reasonable.
    n_splits = min(3, max(2, len(x) // 10))
    splitter = TimeSeriesSplit(n_splits=n_splits)

    all_actuals: list[float] = []
    all_preds: list[float] = []
    folds: list[dict[str, Any]] = []

    for fold_idx, (train_idx, test_idx) in enumerate(splitter.split(x), start=1):
        y_train_fold = y.iloc[train_idx]
        y_test_fold = y.iloc[test_idx]
        steps = len(y_test_fold)

        # Dates for the test window (for visual overlay in the UI).
        date_col = series["date"] if "date" in series.columns else pd.Series(dtype="object")
        test_dates: list[str] = (
            date_col.iloc[test_idx].dt.strftime("%Y-%m-%d").tolist()
            if not date_col.empty
            else []
        )

        try:
            if model_key in {"linear_regression", "xgboost"}:
                x_train_fold = x.iloc[train_idx]
                x_test_fold = x.iloc[test_idx]
                if model_key == "linear_regression":
                    preds_arr = np.asarray(_run_linear_regression(x_train_fold, y_train_fold, x_test_fold))
                else:
                    preds_arr = np.asarray(_run_xgboost(x_train_fold, y_train_fold, x_test_fold))
            else:
                raw = _run_forecast_for_model(
                    model_key=model_key,
                    y_train=y_train_fold,
                    history=series.iloc[train_idx],
                    horizon=steps,
                    granularity=granularity,
                    freq=freq,
                    seasonal_period=seasonal_period,
                    include_promotions=include_promotions,
                    include_price=include_price,
                    include_calendar=include_calendar,
                )
                preds_arr = np.asarray(raw[:steps], dtype=float)
        except Exception as exc:
            logger.warning("Backtest fold %s failed for model '%s': %s", fold_idx, model_key, exc)
            continue

        actuals_arr = y_test_fold.to_numpy(dtype=float)
        n = min(len(actuals_arr), len(preds_arr))
        actuals_arr = actuals_arr[:n]
        preds_arr = preds_arr[:n]

        mae, rmse, mape, smape = _metrics(actuals_arr, preds_arr)
        bias = float(np.mean(preds_arr - actuals_arr))

        folds.append({
            "fold": fold_idx,
            "dates": test_dates[:n],
            "actuals": [round(float(v), 2) for v in actuals_arr],
            "predicted": [round(float(v), 2) for v in preds_arr],
            "mae": round(mae, 3),
            "rmse": round(rmse, 3),
            "mape": round(mape, 3),
            "smape": round(smape, 3),
            "bias": round(bias, 3),
        })
        all_actuals.extend(actuals_arr.tolist())
        all_preds.extend(preds_arr.tolist())

    if not all_actuals:
        return {
            "model": model_key,
            "mae": 0.0, "rmse": 0.0, "mape": 0.0, "smape": 0.0, "bias": 0.0,
            "n_folds": 0, "folds": [],
            "warning": "All backtesting folds failed — check model availability and data size.",
        }

    all_a = np.array(all_actuals)
    all_p = np.array(all_preds)
    mae, rmse, mape, smape = _metrics(all_a, all_p)
    bias = float(np.mean(all_p - all_a))

    return {
        "model": model_key,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "mape": round(mape, 3),
        "smape": round(smape, 3),
        "bias": round(bias, 3),
        "n_folds": len(folds),
        "folds": folds,
    }
