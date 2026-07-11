"""
Anomaly detection service for sales time series.

Two-stage pipeline per the spec (§4.1.2, §3.2.4):
  1. Statistical pass  — Z-score on STL residuals flags spikes/drops/zero-sale periods.
     Using residuals (not raw values) prevents false positives from regular seasonal peaks
     and implicitly removes autocorrelation before applying the Gaussian test.
  2. Isolation Forest  — ML scorer for subtle multivariate anomalies.

Outputs a scored, classified list ready to be served by the anomaly router.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── default tuneable thresholds (overridable per-call via parameters) ──────────
Z_SPIKE_THRESHOLD  = 2.5   # |z| above this → spike / drop
IF_CONTAMINATION   = 0.08  # expected anomaly fraction for Isolation Forest
Z_DROP_THRESHOLD   = -Z_SPIKE_THRESHOLD  # kept for symmetry
IF_RANDOM_STATE    = 42
MIN_ROWS_FOR_IF    = 10    # need at least this many rows to fit IF
ZERO_SALE_THRESHOLD = 0   # nb_ventes == 0 with neighbours > 0 → data-quality flag

# Minimum series length to attempt STL decomposition.
# STL needs at least 2 × period observations.
_STL_MIN_PERIODS_MULTIPLE = 2


def _stl_residuals(sales: np.ndarray, granularity: str) -> np.ndarray:
    """
    Return STL residuals to use for Z-score instead of raw values.

    Seasonal decomposition removes both the trend and the seasonal component so
    the Z-score only fires on genuinely unexpected deviations — not on predictable
    holiday peaks or the usual Ramadan uplift.  Autocorrelation is implicitly
    handled: STL residuals should be approximately white noise, so consecutive
    Z-scores are independent.

    Falls back to raw values when the series is too short or STL fails.
    """
    period = 12 if granularity == "monthly" else 7
    min_len = _STL_MIN_PERIODS_MULTIPLE * period + 1

    if len(sales) < min_len:
        return sales  # too short — use raw values

    try:
        from statsmodels.tsa.seasonal import STL  # already in requirements via statsmodels

        result = STL(pd.Series(sales), period=period, robust=True).fit()
        return result.resid.values.astype(float)
    except Exception as exc:
        logger.debug("STL decomposition failed, falling back to raw values: %s", exc)
        return sales


@dataclass
class AnomalyRecord:
    id: str
    service_code: str
    region_label: str
    detected_date: str          # ISO date string
    anomaly_type: str           # "Unexpected Spike" | "Unexpected Drop" | "Data Quality Issue" | "Gradual Anomaly"
    severity: str               # "high" | "medium"
    expected: float
    actual: float
    variance_pct: float         # signed %
    anomaly_score: float        # 0–1, higher = more anomalous
    z_score: float
    possible_cause: str
    action_recommended: str
    detection_method: str       # "statistical" | "isolation_forest" | "combined"
    rag_explanation: Optional[str] = None
    rag_sources: list[str] = field(default_factory=list)


# ── SQL helpers ────────────────────────────────────────────────────────────────

def _load_sales_series(
    db: Session,
    service_code: str | None,
    region: str | None,
    granularity: str,
) -> pd.DataFrame:
    """Load aggregated sales from mart.vw_monthly_sales_forecasting or daily fact_ventes."""
    if granularity == "monthly":
        query = """
            SELECT
                month_start          AS date,
                service_code,
                region_label,
                SUM(nb_ventes)       AS nb_ventes,
                AVG(prix_moyen)      AS prix_moyen
            FROM mart.vw_monthly_sales_forecasting
            WHERE 1=1
        """
    else:
        query = """
            SELECT
                t.date::date                                            AS date,
                s.service_code,
                COALESCE(g.governorate, g.city, 'UNKNOWN')              AS region_label,
                COUNT(*)                                                AS nb_ventes,
                AVG(COALESCE(o.price, 0))                               AS prix_moyen
            FROM mart.fact_ventes v
            JOIN mart.dim_temps      t ON v.date_id    = t.date_id
            JOIN mart.dim_services   s ON v.service_id = s.service_id
            LEFT JOIN mart.dim_geographie g ON v.geo_id = g.geo_id
            LEFT JOIN mart.dim_offres     o ON v.offre_id = o.offre_id
            WHERE 1=1
        """

    params: dict = {}
    if service_code:
        query += " AND service_code = :service_code"
        params["service_code"] = service_code
    if region:
        if granularity == "monthly":
            query += " AND region_label ILIKE :region"
        else:
            query += " AND COALESCE(g.governorate, g.city, 'UNKNOWN') ILIKE :region"
        params["region"] = f"%{region}%"

    if granularity == "monthly":
        query += " GROUP BY month_start, service_code, region_label ORDER BY month_start ASC"
    else:
        query += " GROUP BY t.date, s.service_code, COALESCE(g.governorate, g.city, 'UNKNOWN') ORDER BY t.date ASC"

    rows = db.execute(text(query), params).fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["date", "service_code", "region_label", "nb_ventes", "prix_moyen"])
    df["date"] = pd.to_datetime(df["date"])
    df["nb_ventes"] = pd.to_numeric(df["nb_ventes"], errors="coerce").fillna(0)
    df["prix_moyen"] = pd.to_numeric(df["prix_moyen"], errors="coerce").fillna(0)
    return df.sort_values("date").reset_index(drop=True)


# ── detection logic ────────────────────────────────────────────────────────────

def _expected_value(series: pd.Series, idx: int, window: int = 3) -> float:
    """Rolling window mean excluding the current index as a simple expected value."""
    indices = [i for i in range(max(0, idx - window), idx)] + [i for i in range(idx + 1, min(len(series), idx + window + 1))]
    if not indices:
        return float(series.mean())
    return float(series.iloc[indices].mean())


def _classify_type(z: float, actual: float, expected: float, z_threshold: float = Z_SPIKE_THRESHOLD) -> str:
    if actual == 0 and expected > 0:
        return "Data Quality Issue"
    if z >= z_threshold:
        return "Unexpected Spike"
    if z <= -z_threshold:
        return "Unexpected Drop"
    return "Gradual Anomaly"


def _severity(z: float, z_threshold: float = Z_SPIKE_THRESHOLD) -> str:
    high_cutoff = z_threshold * 1.2   # 20 % above spike threshold → high severity
    return "high" if abs(z) >= high_cutoff else "medium"


def _probable_cause(anomaly_type: str, z: float, service: str) -> str:
    causes = {
        "Data Quality Issue": f"Possible data entry error or system outage for {service} — zero sales recorded while neighbours are non-zero.",
        "Unexpected Spike": f"Flash promotion, viral marketing impact, or bulk dealer activation detected for {service}.",
        "Unexpected Drop": f"Possible stock rupture, supply chain disruption, or network incident for {service}.",
        "Gradual Anomaly": f"Seasonal shift, competitive pressure, or gradual churn pattern detected for {service}.",
    }
    return causes.get(anomaly_type, "Unknown cause — manual investigation recommended.")


def _recommended_action(anomaly_type: str) -> str:
    actions = {
        "Data Quality Issue": "Validate data source integrity and correct if necessary. Check ETL pipeline for ingestion errors.",
        "Unexpected Spike": "Verify promotion activity and adjust forecast. Ensure stock levels are sufficient to sustain demand.",
        "Unexpected Drop": "Check inventory status, logistics pipeline and network health. Escalate to supply chain team.",
        "Gradual Anomaly": "Monitor trend over next 7 days. Cross-reference with competitor activity and pricing changes.",
    }
    return actions.get(anomaly_type, "Investigate manually.")


def _statistical_anomalies(
    group_df: pd.DataFrame,
    service: str,
    region: str,
    granularity: str = "monthly",
    z_threshold: float = Z_SPIKE_THRESHOLD,
) -> list[AnomalyRecord]:
    """
    Z-score detection on STL residuals for a single (service, region) group.

    Computing Z-scores on the STL residual (trend + seasonality removed) means:
    - Regular seasonal peaks no longer trigger false positives.
    - Autocorrelation is removed because STL residuals are approximately white noise.
    """
    records: list[AnomalyRecord] = []
    sales = group_df["nb_ventes"].to_numpy(dtype=float)

    if len(sales) < 3:
        return records

    residuals = _stl_residuals(sales, granularity)

    res_mean = float(np.mean(residuals))
    res_std  = float(np.std(residuals, ddof=1)) or 1.0
    z_scores = (residuals - res_mean) / res_std

    sales_mean = float(np.mean(sales))

    for i, (z, actual) in enumerate(zip(z_scores, sales)):
        is_zero_anomaly = (actual == 0 and sales_mean > 0)
        is_stat_anomaly = abs(z) >= z_threshold

        if not (is_stat_anomaly or is_zero_anomaly):
            continue

        row = group_df.iloc[i]
        expected = _expected_value(pd.Series(sales), i)
        variance_pct = ((actual - expected) / expected * 100) if expected != 0 else -100.0
        anomaly_type = _classify_type(z, actual, expected, z_threshold)
        severity = _severity(z if not is_zero_anomaly else -(z_threshold * 1.6), z_threshold)

        anomaly_score = min(1.0, abs(z) / (z_threshold * 2.0))
        if is_zero_anomaly:
            anomaly_score = 0.9

        records.append(AnomalyRecord(
            id=f"stat_{service}_{region}_{i}",
            service_code=service,
            region_label=region,
            detected_date=str(row["date"].date()),
            anomaly_type=anomaly_type,
            severity=severity,
            expected=round(expected, 2),
            actual=round(actual, 2),
            variance_pct=round(variance_pct, 1),
            anomaly_score=round(anomaly_score, 3),
            z_score=round(float(z), 3),
            possible_cause=_probable_cause(anomaly_type, z, service),
            action_recommended=_recommended_action(anomaly_type),
            detection_method="statistical",
        ))

    return records


def _isolation_forest_anomalies(
    group_df: pd.DataFrame,
    service: str,
    region: str,
    granularity: str = "monthly",
    if_contamination: float = IF_CONTAMINATION,
    z_threshold: float = Z_SPIKE_THRESHOLD,
) -> list[AnomalyRecord]:
    """Isolation Forest scorer — adds IF-only anomalies not caught statistically."""
    records: list[AnomalyRecord] = []
    if len(group_df) < MIN_ROWS_FOR_IF:
        return records

    sales = group_df["nb_ventes"].to_numpy(dtype=float)
    residuals = _stl_residuals(sales, granularity)

    features = group_df[["nb_ventes", "prix_moyen"]].copy()
    features["month"] = group_df["date"].dt.month
    features["day_of_year"] = group_df["date"].dt.dayofyear
    features["residual"] = residuals   # give IF direct access to the seasonality-adjusted signal

    scaler = StandardScaler()
    X = scaler.fit_transform(features.fillna(0))

    clf = IsolationForest(contamination=if_contamination, random_state=IF_RANDOM_STATE, n_estimators=100)
    clf.fit(X)
    preds  = clf.predict(X)        # -1 = anomaly, 1 = normal
    scores = clf.score_samples(X)  # more negative = more anomalous

    # Normalise IF scores to 0–1 range (higher = more anomalous)
    s_min, s_max = scores.min(), scores.max()
    denom = (s_max - s_min) or 1.0
    norm_scores = 1.0 - (scores - s_min) / denom

    res_mean = float(np.mean(residuals))
    res_std  = float(np.std(residuals, ddof=1)) or 1.0

    for i, (pred, ns) in enumerate(zip(preds, norm_scores)):
        if pred != -1 or ns < 0.55:
            continue

        row = group_df.iloc[i]
        actual   = float(sales[i])
        expected = _expected_value(pd.Series(sales), i)
        # Use residual-based z for classification consistency with statistical pass
        z = float((residuals[i] - res_mean) / res_std)
        variance_pct = ((actual - expected) / expected * 100) if expected != 0 else -100.0
        anomaly_type = _classify_type(z, actual, expected, z_threshold)

        records.append(AnomalyRecord(
            id=f"if_{service}_{region}_{i}",
            service_code=service,
            region_label=region,
            detected_date=str(row["date"].date()),
            anomaly_type=anomaly_type,
            severity="medium",
            expected=round(expected, 2),
            actual=round(actual, 2),
            variance_pct=round(variance_pct, 1),
            anomaly_score=round(float(ns), 3),
            z_score=round(float(z), 3),
            possible_cause=_probable_cause(anomaly_type, z, service),
            action_recommended=_recommended_action(anomaly_type),
            detection_method="isolation_forest",
        ))

    return records


def _dedup(records: list[AnomalyRecord]) -> list[AnomalyRecord]:
    """Remove duplicate anomalies for same (service, region, date) — keep highest score."""
    seen: dict[tuple, AnomalyRecord] = {}
    for r in records:
        key = (r.service_code, r.region_label, r.detected_date)
        if key not in seen or r.anomaly_score > seen[key].anomaly_score:
            if key in seen:
                # Merge: mark as combined detection
                r.detection_method = "combined"
            seen[key] = r
    return sorted(seen.values(), key=lambda x: x.anomaly_score, reverse=True)


# ── public API ─────────────────────────────────────────────────────────────────

def detect_anomalies(
    db: Session,
    service_code: str | None = None,
    region: str | None = None,
    severity_filter: str | None = None,
    anomaly_type_filter: str | None = None,
    granularity: str = "monthly",
    limit: int = 50,
    z_threshold: float = Z_SPIKE_THRESHOLD,
    if_contamination: float = IF_CONTAMINATION,
) -> list[AnomalyRecord]:
    """
    Run full anomaly detection pipeline on sales data from the data mart.

    Returns deduplicated, scored, classified anomaly records.
    """
    df = _load_sales_series(db, service_code, region, granularity)
    if df.empty:
        logger.warning("No sales data found for anomaly detection.")
        return []

    all_records: list[AnomalyRecord] = []

    # Run detection per (service_code, region_label) group
    for (svc, reg), group in df.groupby(["service_code", "region_label"]):
        group = group.reset_index(drop=True)
        all_records.extend(_statistical_anomalies(group, str(svc), str(reg), granularity=granularity, z_threshold=z_threshold))
        all_records.extend(_isolation_forest_anomalies(group, str(svc), str(reg), granularity=granularity, if_contamination=if_contamination, z_threshold=z_threshold))

    results = _dedup(all_records)

    # Apply filters
    if severity_filter and severity_filter != "all":
        results = [r for r in results if r.severity == severity_filter]
    if anomaly_type_filter and anomaly_type_filter != "all":
        results = [r for r in results if r.anomaly_type == anomaly_type_filter]

    return results[:limit]


def get_timeseries(
    db: Session,
    service_code: str | None = None,
    region: str | None = None,
    granularity: str = "monthly",
) -> list[dict]:
    """Return full time series with per-point anomaly flags for chart rendering."""
    df = _load_sales_series(db, service_code, region, granularity)
    if df.empty:
        return []

    # Aggregate by date across groups so chart always shows one line
    agg = (
        df.groupby("date")
        .agg(nb_ventes=("nb_ventes", "sum"), prix_moyen=("prix_moyen", "mean"))
        .reset_index()
        .sort_values("date")
    )

    # Re-run detection to get anomaly flags; use generous limit (default thresholds OK for chart)
    records = detect_anomalies(db, service_code=service_code, region=region, granularity=granularity, limit=500)
    # When multiple groups are merged, keep the highest-score record per date
    anomaly_map: dict[str, AnomalyRecord] = {}
    for r in records:
        key = r.detected_date
        if key not in anomaly_map or r.anomaly_score > anomaly_map[key].anomaly_score:
            anomaly_map[key] = r

    result = []
    for _, row in agg.iterrows():
        date_str = str(row["date"].date())
        rec = anomaly_map.get(date_str)
        result.append({
            "date": date_str,
            "nb_ventes": float(row["nb_ventes"]),
            "is_anomaly": rec is not None,
            "anomaly_type": rec.anomaly_type if rec else None,
            "severity": rec.severity if rec else None,
            "z_score": round(rec.z_score, 2) if rec else None,
            "expected": round(rec.expected, 0) if rec else None,
        })

    return result


def get_summary_stats(records: list[AnomalyRecord]) -> dict:
    """Compute dashboard summary cards from a list of anomaly records."""
    if not records:
        return {
            "total": 0,
            "high_severity": 0,
            "medium_severity": 0,
            "spikes": 0,
            "drops": 0,
            "data_quality": 0,
            "detection_accuracy_pct": 0.0,
        }

    high   = sum(1 for r in records if r.severity == "high")
    medium = sum(1 for r in records if r.severity == "medium")
    combined = sum(1 for r in records if r.detection_method == "combined")
    accuracy = round((combined / len(records)) * 100, 1) if records else 0.0

    return {
        "total": len(records),
        "high_severity": high,
        "medium_severity": medium,
        "spikes":       sum(1 for r in records if r.anomaly_type == "Unexpected Spike"),
        "drops":        sum(1 for r in records if r.anomaly_type == "Unexpected Drop"),
        "data_quality": sum(1 for r in records if r.anomaly_type == "Data Quality Issue"),
        "detection_accuracy_pct": accuracy,
    }
