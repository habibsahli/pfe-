"""
Promo What-If Simulation Engine

Core logic for:
- Estimating sales uplift from historical promotions or elasticity defaults
- LLM-driven uplift adjustment using RAG knowledge base (runs before effect is applied)
- Applying promo uplift + post-promo dip to a baseline forecast
- Calculating safety stock, reorder point, stock gap, and rupture risk
- Enriching results with RAG context (similar past campaigns)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from scipy.stats import norm as scipy_norm
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import engine
from app.services.forecasting_service import generate_forecast
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service

logger = logging.getLogger(__name__)

# ─── market event calendar ───────────────────────────────────────────────────
# Approximate Islamic calendar windows (may shift ±1-2 days based on moon sighting).
# Each tuple is (window_start, window_end) — inclusive.
# Pre-event promotions (e.g. pre-Ramadan) are caught via a 14-day lead buffer
# in detect_promo_event(), so the windows themselves are not artificially widened.

_RAMADAN_WINDOWS: list[tuple[date, date]] = [
    (date(2020, 4, 24), date(2020, 5, 23)),
    (date(2021, 4, 13), date(2021, 5, 12)),
    (date(2022, 4, 2),  date(2022, 5, 1)),
    (date(2023, 3, 23), date(2023, 4, 20)),
    (date(2024, 3, 11), date(2024, 4, 9)),
    (date(2025, 3, 1),  date(2025, 3, 29)),
    (date(2026, 2, 18), date(2026, 3, 19)),
    (date(2027, 2, 8),  date(2027, 3, 8)),
    (date(2028, 1, 28), date(2028, 2, 25)),
    (date(2029, 1, 17), date(2029, 2, 14)),
    (date(2030, 1, 6),  date(2030, 2, 4)),
]

_EID_FITR_WINDOWS: list[tuple[date, date]] = [
    (date(2020, 5, 24), date(2020, 5, 30)),
    (date(2021, 5, 13), date(2021, 5, 19)),
    (date(2022, 5, 2),  date(2022, 5, 8)),
    (date(2023, 4, 21), date(2023, 4, 27)),
    (date(2024, 4, 10), date(2024, 4, 16)),
    (date(2025, 3, 30), date(2025, 4, 5)),
    (date(2026, 3, 20), date(2026, 3, 26)),
    (date(2027, 3, 9),  date(2027, 3, 15)),
    (date(2028, 2, 26), date(2028, 3, 3)),
    (date(2029, 2, 15), date(2029, 2, 21)),
    (date(2030, 2, 5),  date(2030, 2, 11)),
]

_EID_ADHA_WINDOWS: list[tuple[date, date]] = [
    (date(2020, 7, 31), date(2020, 8, 6)),
    (date(2021, 7, 20), date(2021, 7, 26)),
    (date(2022, 7, 9),  date(2022, 7, 15)),
    (date(2023, 6, 28), date(2023, 7, 4)),
    (date(2024, 6, 16), date(2024, 6, 22)),
    (date(2025, 6, 6),  date(2025, 6, 12)),
    (date(2026, 5, 27), date(2026, 6, 2)),
    (date(2027, 5, 16), date(2027, 5, 22)),
    (date(2028, 5, 5),  date(2028, 5, 11)),
    (date(2029, 4, 24), date(2029, 4, 30)),
    (date(2030, 4, 14), date(2030, 4, 20)),
]

# ─── Tunisian commercial event calendar ──────────────────────────────────────
# Fixed-date national events: (name, month, day_start, month_end, day_end)
# These repeat every year on the same dates.
_TUNISIAN_FIXED_EVENTS: list[tuple[str, int, int, int, int]] = [
    # Journée de la Révolution — January 14 (±5 days)
    ("revolution",          1,  9,  1, 19),
    # Nouvel An — January 1 (±4 days, does not bleed into Revolution week)
    ("nouvel_an",           12, 28,  1,  7),
    # Fête de l'Indépendance — March 20 (±5 days)
    ("fete_independance",   3,  15,  3, 25),
    # Fête Nationale (Fête de la République) — July 25 (±4 days)
    ("fete_nationale",      7,  21,  7, 29),
    # Rentrée scolaire — mid-August through September
    ("rentree_scolaire",    8,  16,  9, 30),
    # Vacances d'été — mid-June through mid-August (lower priority than fete_nationale)
    ("ete",                 6,  15,  8, 15),
]

# Human-readable labels for the UI
EVENT_LABELS: dict[str, str] = {
    "ramadan":           "Ramadan",
    "eid_fitr":          "Aïd el-Fitr",
    "eid_adha":          "Aïd el-Adha",
    "revolution":        "Journée de la Révolution",
    "nouvel_an":         "Nouvel An",
    "fete_independance": "Fête de l'Indépendance",
    "fete_nationale":    "Fête Nationale",
    "rentree_scolaire":  "Rentrée scolaire",
    "ete":               "Vacances d'été",
}


def _fixed_event_overlaps(
    name: str, sm: int, sd: int, em: int, ed: int,
    buffered_start: date, promo_end: date,
) -> bool:
    """Check overlap for a fixed-date event that may wrap across year boundary (e.g. Dec 28 → Jan 7)."""
    year = buffered_start.year
    wraps = (sm, sd) > (em, ed)  # e.g. Dec 28 → Jan 7 wraps the year

    if wraps:
        # Check against current-year start window and next-year end window
        candidates = [
            (date(year, sm, sd),     date(year + 1, em, ed)),
            (date(year - 1, sm, sd), date(year, em, ed)),
        ]
    else:
        candidates = [
            (date(year, sm, sd), date(year, em, ed)),
            (date(year - 1, sm, sd), date(year - 1, em, ed)),
        ]

    return any(buffered_start <= ev_end and promo_end >= ev_start for ev_start, ev_end in candidates)


def detect_promo_event(promo_start: date, promo_end: date) -> str | None:
    """
    Return the dominant Tunisian market event tag for a promo window, or None.

    A 14-day lead buffer before promo_start catches pre-event campaigns
    (e.g. a promo launched 10 days before Ramadan to capture the shopping surge).

    Priority: Eid el-Fitr > Eid el-Adha > Ramadan > fixed national/seasonal events
    (in the order defined in _TUNISIAN_FIXED_EVENTS).
    """
    buffered_start = promo_start - timedelta(days=14)

    def islamic_overlaps(windows: list[tuple[date, date]]) -> bool:
        return any(buffered_start <= ev_end and promo_end >= ev_start for ev_start, ev_end in windows)

    if islamic_overlaps(_EID_FITR_WINDOWS):
        return "eid_fitr"
    if islamic_overlaps(_EID_ADHA_WINDOWS):
        return "eid_adha"
    if islamic_overlaps(_RAMADAN_WINDOWS):
        return "ramadan"

    for name, sm, sd, em, ed in _TUNISIAN_FIXED_EVENTS:
        if _fixed_event_overlaps(name, sm, sd, em, ed, buffered_start, promo_end):
            return name

    return None


def _build_event_backfill_sql() -> str:
    """Generate a SQL CASE statement that assigns event_type to existing rows from their dates."""
    cases: list[str] = []
    # Islamic events (date-specific, checked first)
    for ev_start, ev_end in _EID_FITR_WINDOWS:
        cases.append(f"  WHEN promo_start BETWEEN '{ev_start}' AND '{ev_end}' THEN 'eid_fitr'")
    for ev_start, ev_end in _EID_ADHA_WINDOWS:
        cases.append(f"  WHEN promo_start BETWEEN '{ev_start}' AND '{ev_end}' THEN 'eid_adha'")
    for ev_start, ev_end in _RAMADAN_WINDOWS:
        cases.append(f"  WHEN promo_start BETWEEN '{ev_start}' AND '{ev_end}' THEN 'ramadan'")
    # Fixed Tunisian events (month/day ranges, same every year)
    cases += [
        # Journée de la Révolution (Jan 9–19)
        "  WHEN EXTRACT(MONTH FROM promo_start) = 1 AND EXTRACT(DAY FROM promo_start) BETWEEN 9 AND 19 THEN 'revolution'",
        # Nouvel An (Dec 28–31 or Jan 1–7, excluding Revolution window)
        "  WHEN EXTRACT(MONTH FROM promo_start) = 12 AND EXTRACT(DAY FROM promo_start) >= 28 THEN 'nouvel_an'",
        "  WHEN EXTRACT(MONTH FROM promo_start) = 1 AND EXTRACT(DAY FROM promo_start) <= 7 THEN 'nouvel_an'",
        # Fête de l'Indépendance (Mar 15–25)
        "  WHEN EXTRACT(MONTH FROM promo_start) = 3 AND EXTRACT(DAY FROM promo_start) BETWEEN 15 AND 25 THEN 'fete_independance'",
        # Fête Nationale (Jul 21–29)
        "  WHEN EXTRACT(MONTH FROM promo_start) = 7 AND EXTRACT(DAY FROM promo_start) BETWEEN 21 AND 29 THEN 'fete_nationale'",
        # Rentrée scolaire (Aug 16 – Sep 30)
        "  WHEN EXTRACT(MONTH FROM promo_start) = 9 THEN 'rentree_scolaire'",
        "  WHEN EXTRACT(MONTH FROM promo_start) = 8 AND EXTRACT(DAY FROM promo_start) >= 16 THEN 'rentree_scolaire'",
        # Vacances d'été (Jun 15 – Aug 15)
        "  WHEN EXTRACT(MONTH FROM promo_start) IN (6, 7) THEN 'ete'",
        "  WHEN EXTRACT(MONTH FROM promo_start) = 8 AND EXTRACT(DAY FROM promo_start) <= 15 THEN 'ete'",
    ]
    lines = "\nCASE\n" + "\n".join(cases) + "\n  ELSE NULL\nEND"
    return f"UPDATE fact_promotions SET event_type = {lines} WHERE event_type IS NULL"


# ─── table bootstrap ────────────────────────────────────────────────────────

_TABLES_CREATED = False

_DDL = """
CREATE TABLE IF NOT EXISTS fact_promotions (
    id SERIAL PRIMARY KEY,
    service_type VARCHAR(50) NOT NULL,
    region VARCHAR(100),
    discount_percent FLOAT NOT NULL,
    promo_start DATE NOT NULL,
    promo_end DATE NOT NULL,
    channel VARCHAR(50),
    event_type VARCHAR(50),
    actual_uplift_percent FLOAT,
    units_sold_during FLOAT,
    baseline_units_expected FLOAT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fact_promotions_service
    ON fact_promotions(service_type, discount_percent);
CREATE INDEX IF NOT EXISTS idx_fact_promotions_event
    ON fact_promotions(event_type);

CREATE TABLE IF NOT EXISTS promo_elasticity (
    id SERIAL PRIMARY KEY,
    service_type VARCHAR(50),
    discount_min FLOAT NOT NULL,
    discount_max FLOAT NOT NULL,
    expected_uplift_percent FLOAT NOT NULL,
    post_promo_dip_percent FLOAT NOT NULL DEFAULT 0.30,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS whatif_scenarios (
    id SERIAL PRIMARY KEY,
    scenario_name VARCHAR(200),
    request_params JSONB NOT NULL,
    results JSONB NOT NULL,
    rag_explanation TEXT,
    rag_sources JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_whatif_scenarios_created
    ON whatif_scenarios(created_at DESC);
"""

_ELASTICITY_SEED_GENERIC = [
    {"service_type": None, "discount_min": 5,  "discount_max": 10, "expected_uplift_percent": 10.0, "post_promo_dip_percent": 0.20},
    {"service_type": None, "discount_min": 10, "discount_max": 20, "expected_uplift_percent": 20.0, "post_promo_dip_percent": 0.25},
    {"service_type": None, "discount_min": 20, "discount_max": 30, "expected_uplift_percent": 30.0, "post_promo_dip_percent": 0.30},
    {"service_type": None, "discount_min": 30, "discount_max": 50, "expected_uplift_percent": 45.0, "post_promo_dip_percent": 0.35},
]

# Service-specific elasticity rows calibrated from Tunisian telecom market patterns.
# FIBRE: high-value subscription, committed base → moderate elasticity, shallow post-promo dip.
# 5G: premium tier, tech-early adopters → lowest price sensitivity.
# DATA_BUNDLE: mass market, many substitutes, high impulse factor → strongest elasticity.
# VOD: discretionary entertainment, seasonal demand spikes → moderate-to-high elasticity.
_ELASTICITY_SEED_SERVICES = [
    {"service_type": "FIBRE",       "discount_min": 5,  "discount_max": 10, "expected_uplift_percent":  8.0, "post_promo_dip_percent": 0.15},
    {"service_type": "FIBRE",       "discount_min": 10, "discount_max": 20, "expected_uplift_percent": 15.0, "post_promo_dip_percent": 0.18},
    {"service_type": "FIBRE",       "discount_min": 20, "discount_max": 30, "expected_uplift_percent": 22.0, "post_promo_dip_percent": 0.22},
    {"service_type": "FIBRE",       "discount_min": 30, "discount_max": 50, "expected_uplift_percent": 30.0, "post_promo_dip_percent": 0.25},
    {"service_type": "5G",          "discount_min": 5,  "discount_max": 10, "expected_uplift_percent":  6.0, "post_promo_dip_percent": 0.12},
    {"service_type": "5G",          "discount_min": 10, "discount_max": 20, "expected_uplift_percent": 12.0, "post_promo_dip_percent": 0.15},
    {"service_type": "5G",          "discount_min": 20, "discount_max": 30, "expected_uplift_percent": 20.0, "post_promo_dip_percent": 0.20},
    {"service_type": "5G",          "discount_min": 30, "discount_max": 50, "expected_uplift_percent": 28.0, "post_promo_dip_percent": 0.22},
    {"service_type": "DATA_BUNDLE", "discount_min": 5,  "discount_max": 10, "expected_uplift_percent": 15.0, "post_promo_dip_percent": 0.25},
    {"service_type": "DATA_BUNDLE", "discount_min": 10, "discount_max": 20, "expected_uplift_percent": 28.0, "post_promo_dip_percent": 0.30},
    {"service_type": "DATA_BUNDLE", "discount_min": 20, "discount_max": 30, "expected_uplift_percent": 40.0, "post_promo_dip_percent": 0.35},
    {"service_type": "DATA_BUNDLE", "discount_min": 30, "discount_max": 50, "expected_uplift_percent": 55.0, "post_promo_dip_percent": 0.40},
    {"service_type": "VOD",         "discount_min": 5,  "discount_max": 10, "expected_uplift_percent": 12.0, "post_promo_dip_percent": 0.20},
    {"service_type": "VOD",         "discount_min": 10, "discount_max": 20, "expected_uplift_percent": 22.0, "post_promo_dip_percent": 0.25},
    {"service_type": "VOD",         "discount_min": 20, "discount_max": 30, "expected_uplift_percent": 32.0, "post_promo_dip_percent": 0.30},
    {"service_type": "VOD",         "discount_min": 30, "discount_max": 50, "expected_uplift_percent": 42.0, "post_promo_dip_percent": 0.35},
]


def _ensure_tables() -> None:
    global _TABLES_CREATED
    if _TABLES_CREATED:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(_DDL))

            # Migration: add event_type column to existing tables that predate this column
            conn.execute(text(
                "ALTER TABLE fact_promotions ADD COLUMN IF NOT EXISTS event_type VARCHAR(50)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_fact_promotions_event ON fact_promotions(event_type)"
            ))

            # Backfill event_type for any rows where it is still NULL
            conn.execute(text(_build_event_backfill_sql()))

            _INSERT_ELASTICITY = text(
                "INSERT INTO promo_elasticity "
                "(service_type, discount_min, discount_max, expected_uplift_percent, post_promo_dip_percent) "
                "VALUES (:service_type, :discount_min, :discount_max, :expected_uplift_percent, :post_promo_dip_percent)"
            )
            # Seed generic fallback rows only if table is completely empty
            count = conn.execute(
                text("SELECT COUNT(*) FROM promo_elasticity")
            ).scalar()
            if count == 0:
                conn.execute(_INSERT_ELASTICITY, _ELASTICITY_SEED_GENERIC)

            # Seed service-specific rows idempotently (migration for existing installs)
            svc_count = conn.execute(
                text("SELECT COUNT(*) FROM promo_elasticity WHERE service_type IS NOT NULL")
            ).scalar()
            if svc_count == 0:
                conn.execute(_INSERT_ELASTICITY, _ELASTICITY_SEED_SERVICES)
        _TABLES_CREATED = True
    except Exception as exc:
        logger.warning("Promo table bootstrap failed: %s", exc)


# ─── uplift estimation ───────────────────────────────────────────────────────

def _query_historical_uplift(
    db: Session,
    service_type: str,
    discount_percent: float,
    promo_month: int,
    region: str | None = None,
    channel: str | None = None,
    event_type: str | None = None,
) -> tuple[float, int]:
    """
    Return (avg_uplift_pct, count) from fact_promotions.

    Priority ladder (most specific → broadest):
      When event detected (e.g. ramadan, eid_fitr …):
        1.  event + region + channel
        2.  event + region
        3.  event  (any region/channel)
      Always tried as fallback:
        4.  season ±1 month + region + channel
        5.  season ±1 month + region
        6.  season ±1 month
        7.  (broadest) any time, any region, any channel

    Stops at the first level that yields ≥2 matches (strong signal).
    Falls back to ≥1 match if nothing yields ≥2.
    Dip factor is NOT returned — always sourced from elasticity table.
    """
    base_params: dict[str, Any] = {
        "service_type": service_type.upper(),
        "disc_lo": max(0.0, discount_percent - 7),
        "disc_hi": discount_percent + 7,
    }
    months_lo = max(1, promo_month - 1)
    months_hi = min(12, promo_month + 1)

    # Each attempt is (extra WHERE clause fragment, params dict)
    attempts: list[tuple[str, dict[str, Any]]] = []

    # ── Event-based passes (only when a named event is detected) ──────────────
    if event_type:
        p_ev = {**base_params, "event_type": event_type}

        c = "AND event_type = :event_type"
        if region:
            c += " AND region = :region"
            p_ev = {**p_ev, "region": region}
        if channel:
            c += " AND channel = :channel"
            p_ev = {**p_ev, "channel": channel}
        attempts.append((c, p_ev))                           # 1: event+region+channel

        if channel:                                           # 2: event+region (drop channel)
            p2 = {**base_params, "event_type": event_type}
            c2 = "AND event_type = :event_type"
            if region:
                c2 += " AND region = :region"
                p2["region"] = region
            attempts.append((c2, p2))

        if region:                                            # 3: event only
            attempts.append(("AND event_type = :event_type", {**base_params, "event_type": event_type}))
        elif not channel:
            attempts.append(("AND event_type = :event_type", {**base_params, "event_type": event_type}))

    # ── Season-based passes (±1 month, calendar-aware) ────────────────────────
    p_s = {**base_params, "month_lo": months_lo, "month_hi": months_hi}
    cs = "AND EXTRACT(MONTH FROM promo_start) BETWEEN :month_lo AND :month_hi"

    c4 = cs
    p4 = {**p_s}
    if region:
        c4 += " AND region = :region"
        p4["region"] = region
    if channel:
        c4 += " AND channel = :channel"
        p4["channel"] = channel
    attempts.append((c4, p4))                                 # 4: season+region+channel

    if channel:                                               # 5: season+region
        p5 = {**p_s}
        c5 = cs
        if region:
            c5 += " AND region = :region"
            p5["region"] = region
        attempts.append((c5, p5))

    if region:                                                # 6: season only
        attempts.append((cs, p_s))
    elif not channel:
        attempts.append((cs, p_s))

    attempts.append(("", base_params))                        # 7: broadest

    def _run(clause: str, params: dict) -> tuple[float, int]:
        row = db.execute(
            text(f"""
                SELECT AVG(actual_uplift_percent) AS avg_uplift, COUNT(*) AS cnt
                FROM fact_promotions
                WHERE service_type = :service_type
                  AND actual_uplift_percent IS NOT NULL
                  AND discount_percent BETWEEN :disc_lo AND :disc_hi
                  {clause}
            """),
            params,
        ).mappings().one_or_none()
        if row and row["cnt"] and int(row["cnt"]) >= 1:
            return float(row["avg_uplift"]), int(row["cnt"])
        return 0.0, 0

    # First pass: look for ≥2 matches (strong signal)
    for clause, params in attempts:
        uplift, cnt = _run(clause, params)
        if cnt >= 2:
            return uplift, cnt

    # Second pass: accept ≥1 match as a weak signal
    for clause, params in attempts:
        uplift, cnt = _run(clause, params)
        if cnt >= 1:
            return uplift, cnt

    return 0.0, 0


def _query_similar_campaigns(
    db: Session,
    service_type: str,
    discount_percent: float,
    promo_month: int,
    region: str | None = None,
    channel: str | None = None,
    event_type: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Return individual historical campaigns from fact_promotions for display in the
    simulation response (spec §A: 'historique campagnes similaires').

    Uses the same relaxed discount window as _query_historical_uplift (±7 pp)
    and the same season ±1 month filter, but returns rows rather than an average.
    """
    base_params: dict[str, Any] = {
        "service_type": service_type.upper(),
        "disc_lo": max(0.0, discount_percent - 7),
        "disc_hi": discount_percent + 7,
        "months_lo": max(1, promo_month - 1),
        "months_hi": min(12, promo_month + 1),
        "limit": limit,
    }
    extra = ""
    if event_type:
        extra += " AND event_type = :event_type"
        base_params["event_type"] = event_type
    elif region:
        extra += " AND region = :region"
        base_params["region"] = region

    try:
        rows = db.execute(
            text(f"""
                SELECT
                    service_type, region, channel, event_type,
                    discount_percent, promo_start, promo_end,
                    actual_uplift_percent, units_sold_during,
                    baseline_units_expected, notes
                FROM fact_promotions
                WHERE service_type = :service_type
                  AND actual_uplift_percent IS NOT NULL
                  AND discount_percent BETWEEN :disc_lo AND :disc_hi
                  AND EXTRACT(MONTH FROM promo_start) BETWEEN :months_lo AND :months_hi
                  {extra}
                ORDER BY promo_start DESC
                LIMIT :limit
            """),
            base_params,
        ).mappings().all()
        return [
            {
                "service_type":            r["service_type"],
                "region":                  r["region"],
                "channel":                 r["channel"],
                "event_type":              r["event_type"],
                "discount_percent":        r["discount_percent"],
                "promo_start":             str(r["promo_start"]),
                "promo_end":               str(r["promo_end"]),
                "actual_uplift_percent":   r["actual_uplift_percent"],
                "units_sold_during":       r["units_sold_during"],
                "baseline_units_expected": r["baseline_units_expected"],
                "notes":                   r["notes"],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("Similar campaigns query failed: %s", exc)
        return []


def _lookup_elasticity(
    db: Session,
    service_type: str,
    discount_percent: float,
) -> tuple[float, float]:
    """
    Return (expected_uplift_pct, post_promo_dip_pct) from promo_elasticity.
    Prefers service-specific row; falls back to global (service_type IS NULL).
    """
    row = db.execute(
        text("""
            SELECT expected_uplift_percent, post_promo_dip_percent
            FROM promo_elasticity
            WHERE (service_type = :service_type OR service_type IS NULL)
              AND :discount BETWEEN discount_min AND discount_max
            ORDER BY (service_type IS NULL) ASC
            LIMIT 1
        """),
        {"service_type": service_type.upper(), "discount": discount_percent},
    ).mappings().one_or_none()

    if row:
        return float(row["expected_uplift_percent"]), float(row["post_promo_dip_percent"])

    # Hard-coded fallback if table is empty
    if discount_percent <= 10:
        return 10.0, 0.20
    if discount_percent <= 20:
        return 20.0, 0.25
    if discount_percent <= 30:
        return 30.0, 0.30
    return 45.0, 0.35


# ─── service code mapping ────────────────────────────────────────────────────

# Simulation uses short service names; mart.dim_services uses the DB service_code
_SERVICE_CODE_MAP: dict[str, str] = {
    "DATA": "DATA_BUNDLE",
}


def _to_db_service_code(service_type: str) -> str:
    return _SERVICE_CODE_MAP.get(service_type.upper(), service_type.upper())


# ─── stock snapshot lookup ───────────────────────────────────────────────────

def lookup_stock_snapshot(
    db: Session,
    service_type: str,
    region: str | None = None,
) -> dict[str, Any]:
    """
    Return the latest available stock for a service + optional region from fact_stock.

    Aggregates available_qty across all products belonging to the service.
    Falls back to a sales-velocity estimate when fact_stock has no matching rows.
    """
    params: dict[str, Any] = {"service_code": _to_db_service_code(service_type)}

    outer_region = ""
    inner_region = ""
    if region and region.strip():
        outer_region = "AND LOWER(fs.warehouse_code)  = LOWER(:region)"
        inner_region = "AND LOWER(fs2.warehouse_code) = LOWER(:region)"
        params["region"] = region.strip()

    row = db.execute(
        text(f"""
            SELECT
                SUM(fs.available_qty)           AS available_stock,
                SUM(fs.avg_monthly_sales)        AS monthly_sales,
                COUNT(DISTINCT fs.product_id)    AS product_count,
                MAX(fs.snapshot_date)::date      AS snapshot_date
            FROM mart.fact_stock fs
            JOIN mart.dim_products dp  ON fs.product_id  = dp.product_id
            JOIN mart.dim_services ds  ON dp.service_id  = ds.service_id
            WHERE UPPER(ds.service_code) = :service_code
              {outer_region}
              AND fs.snapshot_date = (
                  SELECT MAX(fs2.snapshot_date)
                  FROM mart.fact_stock fs2
                  JOIN mart.dim_products dp2 ON fs2.product_id = dp2.product_id
                  JOIN mart.dim_services ds2 ON dp2.service_id = ds2.service_id
                  WHERE UPPER(ds2.service_code) = :service_code
                  {inner_region}
              )
        """),
        params,
    ).mappings().one_or_none()

    if row and row["available_stock"] is not None and float(row["available_stock"]) > 0:
        return {
            "available_stock": round(float(row["available_stock"])),
            "avg_monthly_sales": round(float(row["monthly_sales"] or 0), 1),
            "product_count": int(row["product_count"] or 0),
            "snapshot_date": str(row["snapshot_date"]),
            "source": "fact_stock",
            "region": region or "nationale",
        }

    # Fallback: estimate from recent 30-day sales velocity × 3 months of coverage
    sales_row = db.execute(
        text("""
            SELECT COUNT(*) AS monthly_sales
            FROM mart.fact_ventes v
            JOIN mart.dim_services s ON v.service_id = s.service_id
            WHERE UPPER(s.service_code) = :service_code
              AND v.created_at >= NOW() - INTERVAL '30 days'
        """),
        {"service_code": _to_db_service_code(service_type)},
    ).mappings().one_or_none()

    monthly = int(sales_row["monthly_sales"]) if sales_row else 0
    estimated = max(30, monthly * 3)
    return {
        "available_stock": float(estimated),
        "avg_monthly_sales": float(monthly),
        "product_count": 0,
        "snapshot_date": None,
        "source": "estimated_from_sales",
        "region": region or "nationale",
    }


# ─── forecast helpers ────────────────────────────────────────────────────────

def _get_baseline_forecast(
    db: Session,
    service_type: str,
    promo_start: date,
    promo_end: date,
    lead_time_days: int,
    fallback_daily_demand: float = 100.0,
) -> tuple[list[dict[str, Any]], bool]:
    """
    Generate a daily baseline forecast covering:
      4 weeks before promo → promo period → 2 weeks post-dip + lead_time buffer.

    Returns (points, is_synthetic). When no sales data exists in the DB, falls back
    to a flat synthetic baseline at fallback_daily_demand units/day so the simulation
    can still run and produce meaningful stock/risk indicators.
    """
    window_start = promo_start - timedelta(days=28)
    window_end = promo_end + timedelta(days=14 + lead_time_days)
    total_days = (window_end - date.today()).days + 1
    horizon = max(total_days, 45)

    db_service_code = _to_db_service_code(service_type)

    payload: dict[str, Any] | None = None
    for model_name in ("prophet", "xgboost", "naive_last"):
        try:
            payload = generate_forecast(
                db=db,
                best_model_name=model_name,
                horizon=horizon,
                service_code=db_service_code,
                granularity="daily",
            )
            break
        except Exception as exc:
            logger.warning("Forecast model %s failed: %s", model_name, exc)

    if payload is not None:
        all_points = payload.get("historical", []) + payload.get("forecast", [])
        result = []
        for pt in all_points:
            pt_date = _parse_date(pt["date"])
            if window_start <= pt_date <= window_end:
                result.append({
                    "date": pt["date"],
                    "value": round(float(pt.get("value", 0)), 2),
                    "lower_bound": round(float(pt.get("lower_bound") or pt.get("value", 0)), 2),
                    "upper_bound": round(float(pt.get("upper_bound") or pt.get("value", 0)), 2),
                })
        if result:
            return sorted(result, key=lambda p: p["date"]), False

    # No sales data at all — generate a synthetic flat baseline from current stock estimate
    logger.warning(
        "No sales data for service=%s; generating synthetic baseline at %.1f units/day",
        service_type, fallback_daily_demand,
    )
    points: list[dict[str, Any]] = []
    cur = window_start
    band = fallback_daily_demand * 0.15
    while cur <= window_end:
        points.append({
            "date": cur.strftime("%Y-%m-%d"),
            "value": round(fallback_daily_demand, 2),
            "lower_bound": round(max(0.0, fallback_daily_demand - band), 2),
            "upper_bound": round(fallback_daily_demand + band, 2),
        })
        cur += timedelta(days=1)
    return points, True


def _parse_date(d: Any) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


# ─── uplift + dip application ────────────────────────────────────────────────

def _apply_promo_effect(
    baseline: list[dict[str, Any]],
    uplift_pct: float,
    dip_factor: float,
    promo_start: date,
    promo_end: date,
) -> list[dict[str, Any]]:
    """
    Returns adjusted forecast:
    - promo window: value × (1 + uplift_pct/100)
    - 2-week post-promo window: value × (1 - uplift_pct/100 × dip_factor)
    - outside: unchanged
    """
    dip_end = promo_end + timedelta(days=14)
    uplift_factor = 1.0 + uplift_pct / 100.0
    dip_factor_applied = 1.0 - (uplift_pct / 100.0) * dip_factor

    adjusted = []
    for pt in baseline:
        pt_date = _parse_date(pt["date"])
        factor = 1.0
        if promo_start <= pt_date <= promo_end:
            factor = uplift_factor
        elif promo_end < pt_date <= dip_end:
            factor = max(0.5, dip_factor_applied)

        adjusted.append({
            "date": pt["date"],
            "value": round(float(pt["value"]) * factor, 2),
            "lower_bound": round(float(pt["lower_bound"]) * factor, 2),
            "upper_bound": round(float(pt["upper_bound"]) * factor, 2),
        })
    return adjusted


# ─── stock indicators ────────────────────────────────────────────────────────

def _stock_indicators(
    adjusted: list[dict[str, Any]],
    current_stock: float,
    lead_time_days: int,
    service_level: float,
    promo_start: date,
    promo_end: date,
) -> dict[str, Any]:
    """Compute safety stock, reorder point, stock gap, coverage, risk."""
    promo_values = [
        float(pt["value"])
        for pt in adjusted
        if promo_start <= _parse_date(pt["date"]) <= promo_end
    ]
    pre_values = [
        float(pt["value"])
        for pt in adjusted
        if _parse_date(pt["date"]) < promo_start
    ]

    avg_daily_before = float(np.mean(pre_values)) if pre_values else 0.0
    avg_daily_during = float(np.mean(promo_values)) if promo_values else avg_daily_before

    promo_duration = max(1, (promo_end - promo_start).days + 1)
    total_promo_demand = sum(promo_values) if promo_values else avg_daily_during * promo_duration

    forecast_errors_std = float(np.std(promo_values)) if len(promo_values) > 1 else avg_daily_during * 0.15
    z = float(scipy_norm.ppf(service_level))
    safety_stock = z * forecast_errors_std * (lead_time_days ** 0.5)
    safety_stock = max(0.0, round(safety_stock, 2))

    reorder_point = (avg_daily_during * lead_time_days) + safety_stock

    stock_required = round(total_promo_demand + safety_stock, 2)
    stock_gap = round(stock_required - current_stock, 2)

    coverage_before = (current_stock / avg_daily_before) if avg_daily_before > 0 else 999.0
    coverage_during = (current_stock / avg_daily_during) if avg_daily_during > 0 else 999.0

    if coverage_during > promo_duration * 1.5:
        rupture_risk = "low"
    elif coverage_during > promo_duration:
        rupture_risk = "medium"
    elif coverage_during > promo_duration * 0.5:
        rupture_risk = "high"
    else:
        rupture_risk = "critical"

    reorder_qty = round(stock_gap + safety_stock, 2) if stock_gap > 0 else None
    order_by = (promo_start - timedelta(days=lead_time_days)).isoformat() if reorder_qty else None
    order_past_due = order_by is not None and _parse_date(order_by) < date.today()

    return {
        "safety_stock": safety_stock,
        "reorder_point": round(reorder_point, 2),
        "stock_required": stock_required,
        "stock_gap": stock_gap,
        "coverage_days_before": round(min(coverage_before, 999.0), 1),
        "coverage_days_during": round(min(coverage_during, 999.0), 1),
        "rupture_risk": rupture_risk,
        "reorder_recommendation": reorder_qty,
        "order_by_date": order_by,
        "order_past_due": order_past_due,
        "avg_daily_demand_during": round(avg_daily_during, 2),
        "total_promo_demand": round(total_promo_demand, 2),
    }


# ─── LLM JSON parsing ────────────────────────────────────────────────────────

_UPLIFT_KEY_ALIASES = (
    "uplift_pct", "uplift_percent", "uplift",
    "expected_uplift_pct", "expected_uplift_percent",
)


def _parse_llm_json(raw: str) -> dict | None:
    """
    Robust 4-stage extraction for the JSON object expected from the promo LLM.

    Stage 1 — strip markdown fences, direct JSON parse.
    Stage 2 — greedy brace-span scan: find all top-level {...} spans via a
              depth counter and try each (largest-first) — handles nested objects
              that defeat simple regex.
    Stage 3 — plain-text key extraction: pull uplift_pct / dip_factor /
              confidence from loose "key: value" patterns when no valid JSON exists.
    Stage 4 — return None so the caller can fall back to the statistical prior.
    """
    if not raw:
        return None

    # Stage 1
    clean = raw.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\s*```\s*$", "", clean, flags=re.MULTILINE)
    try:
        parsed = json.loads(clean)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # Stage 2 — collect all top-level {...} spans
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                spans.append((start, i + 1))
                start = -1
    for s, e in sorted(spans, key=lambda x: x[1] - x[0], reverse=True):
        try:
            parsed = json.loads(raw[s:e])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue

    # Stage 3 — plain-text fallback
    result: dict = {}
    for key in _UPLIFT_KEY_ALIASES:
        m = re.search(rf'"?{re.escape(key)}"?\s*[=:]\s*([0-9.]+)', raw, re.IGNORECASE)
        if m:
            result["uplift_pct"] = float(m.group(1))
            break
    m_dip = re.search(r'"?dip_factor"?\s*[=:]\s*([0-9.]+)', raw, re.IGNORECASE)
    if m_dip:
        result["dip_factor"] = float(m_dip.group(1))
    m_conf = re.search(r'"?confidence"?\s*[=:]\s*"?(low|medium|high)"?', raw, re.IGNORECASE)
    if m_conf:
        result["confidence"] = m_conf.group(1).lower()
    if result.get("uplift_pct") is not None:
        return result

    return None


# ─── LLM-driven RAG enrichment ───────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = (
    "Tu es un expert en promotions commerciales et gestion des stocks dans le secteur télécom tunisien. "
    "Tu réponds UNIQUEMENT avec du JSON valide, sans texte avant ni après, sans bloc markdown. "
    "Tes recommandations doivent être concrètes, actionnables et adaptées au marché télécom tunisien."
)

_LLM_PROMPT_TEMPLATE = """\
{rag_section}

--- Simulation what-if à analyser ---
Service       : {service_type}
Canal         : {channel}
Région        : {region}
Événement     : {event}
Remise        : {discount}%
Durée         : {duration_days} jours ({start} → {end})
Uplift statistique (prior) : {stat_uplift:.1f}% ({hist_label})
Risque rupture stock  : {risk}
Écart stock (gap)     : {gap:+.0f} unités

En te basant sur le contexte documentaire (si disponible) et ton expertise télécom,
retourne EXACTEMENT ce JSON (sans markdown, sans commentaires) :
{{
  "uplift_pct": <float : ton estimation du pourcentage d'uplift attendu>,
  "dip_factor": <float entre 0.10 et 0.50 : intensité du dip post-promo>,
  "confidence": "<low|medium|high>",
  "narrative": "<2-3 phrases résumant les campagnes similaires et les facteurs clés>",
  "recommendations": [
    "<recommandation concrète 1>",
    "<recommandation concrète 2>",
    "<recommandation concrète 3>"
  ]
}}
"""


def _rag_enrich(
    service_type: str,
    region: str | None,
    channel: str | None,
    event_type: str | None,
    discount_percent: float,
    promo_start: date,
    promo_end: date,
    stat_uplift: float,
    stat_dip: float,
    hist_count: int,
    stock_gap: float,
    rupture_risk: str,
) -> tuple[str | None, list[str], list[str], float | None, float | None, str]:
    """
    RAG retrieval + LLM assessment of the simulation scenario.

    Always calls the LLM — RAG context enriches the call but is not required.
    Returns (narrative, sources, recommendations, llm_uplift_pct, llm_dip_factor, confidence).
    """
    duration_days = (promo_end - promo_start).days + 1
    month_name = promo_start.strftime("%B")
    region_str = region or "nationale"
    channel_str = channel or "tous canaux"
    event_str = EVENT_LABELS.get(event_type, "hors événement") if event_type else "hors événement"

    # Enriched query: service + discount + month + duration + region + channel + event
    query = (
        f"promotion {discount_percent:.0f}% service {service_type} {month_name} "
        f"durée {duration_days} jours région {region_str} canal {channel_str} "
        f"événement {event_str} "
        f"historique résultats uplift ventes campagne"
    )

    try:
        chunks = rag_service.retrieve(query, service_type=service_type, top_k=6)
    except Exception as exc:
        logger.warning("RAG retrieve failed: %s", exc)
        chunks = []

    # Lower threshold slightly vs. old code (0.25 vs 0.30) — prefer more context for LLM
    relevant = [c for c in chunks if float(c.get("score", 0)) >= 0.25]

    if relevant:
        context_text = "\n\n".join(
            f"[SOURCE: {c['source']}] {c['text']}" for c in relevant
        )
        rag_section = f"Contexte documentaire récupéré (base de connaissances) :\n{context_text}"
    else:
        # Do NOT bail out — tell the LLM there's no KB context so it uses its own expertise
        rag_section = (
            "Aucun document similaire trouvé dans la base de connaissances. "
            "Base-toi sur ton expertise du secteur télécom tunisien."
        )

    hist_label = (
        f"{hist_count} campagnes historiques similaires"
        if hist_count > 0
        else "table d'élasticité (aucun historique disponible)"
    )

    prompt = _LLM_PROMPT_TEMPLATE.format(
        rag_section=rag_section,
        service_type=service_type.upper(),
        channel=channel_str,
        region=region_str,
        event=event_str,
        discount=discount_percent,
        duration_days=duration_days,
        start=promo_start.isoformat(),
        end=promo_end.isoformat(),
        stat_uplift=stat_uplift,
        hist_label=hist_label,
        risk=rupture_risk,
        gap=stock_gap,
    )

    try:
        raw = ollama_client.generate(
            prompt=prompt,
            system_prompt=_LLM_SYSTEM_PROMPT,
            model=settings.OLLAMA_LLM_MODEL,
            temperature=0.2,
            max_tokens=600,
        )
    except Exception as exc:
        logger.warning("LLM generate failed for RAG enrichment: %s", exc)
        return None, [], [], None, None, "low"

    sources = list({c["source"] for c in relevant})
    parsed = _parse_llm_json(raw or "")

    if not parsed:
        # LLM returned free text — surface it as narrative with no structured overrides
        logger.warning("LLM returned non-JSON output; using as raw narrative")
        return raw, sources, [], None, None, "low"

    narrative: str | None = parsed.get("narrative") or None
    recommendations: list[str] = [
        str(r) for r in (parsed.get("recommendations") or []) if r
    ]
    confidence: str = parsed.get("confidence", "low")
    if confidence not in ("low", "medium", "high"):
        confidence = "low"

    llm_uplift: float | None = None
    llm_dip: float | None = None

    try:
        u = float(parsed["uplift_pct"])
        if 0.0 <= u <= 200.0:
            llm_uplift = round(u, 2)
    except (KeyError, TypeError, ValueError):
        pass

    try:
        d = float(parsed["dip_factor"])
        if 0.05 <= d <= 0.60:
            llm_dip = round(d, 3)
    except (KeyError, TypeError, ValueError):
        pass

    return narrative, sources, recommendations, llm_uplift, llm_dip, confidence


# ─── main simulation function ────────────────────────────────────────────────

def run_promo_simulation(
    db: Session,
    service_type: str,
    discount_percent: float,
    promo_start: date,
    promo_end: date,
    current_stock: float,
    lead_time_days: int = 7,
    service_level: float = 0.95,
    skip_rag: bool = False,
    region: str | None = None,
    channel: str | None = None,
    event_type_override: str | None = None,
) -> dict[str, Any]:
    """
    Full what-if promo simulation. Returns a dict matching PromoSimulationResult schema.

    The LLM is consulted BEFORE the promo effect is applied so it can influence
    the uplift estimate when statistical data is sparse (< 2 historical campaigns).

    event_type_override:
      None       → auto-detect from dates (default behaviour)
      "none"     → force no event (standard promo, no seasonal context)
      any key    → force that event (e.g. "ramadan") regardless of dates
    """
    _ensure_tables()

    promo_month = promo_start.month

    # 0. Resolve market event — override wins over auto-detection
    if event_type_override == "none":
        event_type = None
        logger.info("Event override: standard promo (no event context)")
    elif event_type_override is not None:
        event_type = event_type_override
        logger.info("Event override: forced to '%s'", event_type)
    else:
        event_type = detect_promo_event(promo_start, promo_end)
        if event_type:
            logger.info("Auto-detected market event: %s for promo %s → %s", event_type, promo_start, promo_end)

    # 1. Baseline forecast — falls back to synthetic flat line if no sales data in DB
    fallback_demand = max(1.0, current_stock / 30.0)
    baseline, is_synthetic_baseline = _get_baseline_forecast(
        db, service_type, promo_start, promo_end, lead_time_days, fallback_demand
    )

    # 2. Historical uplift from fact_promotions (event-aware, then region/channel)
    stat_uplift_hist, hist_count = _query_historical_uplift(
        db, service_type, discount_percent, promo_month, region, channel, event_type
    )
    # Fetch individual matching campaigns for the spec §A display requirement.
    similar_campaigns = _query_similar_campaigns(
        db, service_type, discount_percent, promo_month, region, channel, event_type
    )

    # 3. Dip factor always from elasticity table (keyed by discount range, most reliable source)
    stat_uplift_el, stat_dip = _lookup_elasticity(db, service_type, discount_percent)

    if hist_count >= 2:
        uplift_pct = stat_uplift_hist
        uplift_source = "historical"
    else:
        uplift_pct = stat_uplift_el
        uplift_source = "elasticity_table"
    dip_factor = stat_dip  # always from elasticity — never hardcoded

    # 4. LLM-driven assessment — runs BEFORE effect application so it can adjust uplift
    rag_context: str | None = None
    rag_sources: list[str] = []
    rag_recommendations: list[str] = []
    llm_confidence: str | None = None

    if not skip_rag:
        try:
            # Quick rough gap estimate so the LLM prompt has stock risk context
            rough_demand = sum(
                float(pt["value"]) for pt in baseline
                if promo_start <= _parse_date(pt["date"]) <= promo_end
            ) * (1.0 + uplift_pct / 100.0)
            rough_gap = round(rough_demand - current_stock, 2)
            rough_risk = "critical" if rough_gap > current_stock * 0.5 else (
                "high" if rough_gap > 0 else "low"
            )

            (
                rag_context,
                rag_sources,
                rag_recommendations,
                llm_uplift,
                llm_dip,
                llm_confidence,
            ) = _rag_enrich(
                service_type=service_type,
                region=region,
                channel=channel,
                event_type=event_type,
                discount_percent=discount_percent,
                promo_start=promo_start,
                promo_end=promo_end,
                stat_uplift=uplift_pct,
                stat_dip=dip_factor,
                hist_count=hist_count,
                stock_gap=rough_gap,
                rupture_risk=rough_risk,
            )

            # Override with LLM estimate when statistical history is sparse
            # and the LLM expresses at least medium confidence
            if llm_uplift is not None and llm_confidence in ("medium", "high") and hist_count < 2:
                logger.info(
                    "LLM override: uplift %.1f%% → %.1f%% (confidence=%s, hist_count=%d)",
                    uplift_pct, llm_uplift, llm_confidence, hist_count,
                )
                uplift_pct = llm_uplift
                uplift_source = "llm"

            # Always accept LLM dip_factor when it has confidence (better than our stat estimate)
            if llm_dip is not None and llm_confidence in ("medium", "high"):
                dip_factor = llm_dip

        except Exception as exc:
            logger.warning("RAG/LLM enrichment skipped: %s", exc)

    # 5. Apply promo effect using final (possibly LLM-adjusted) uplift + dip
    adjusted = _apply_promo_effect(baseline, uplift_pct, dip_factor, promo_start, promo_end)

    # 6. Additional units
    baseline_promo_sum = sum(
        float(pt["value"])
        for pt in baseline
        if promo_start <= _parse_date(pt["date"]) <= promo_end
    )
    adjusted_promo_sum = sum(
        float(pt["value"])
        for pt in adjusted
        if promo_start <= _parse_date(pt["date"]) <= promo_end
    )
    additional_units = round(adjusted_promo_sum - baseline_promo_sum, 2)

    # 7. Stock indicators (using LLM-adjusted forecast)
    stock = _stock_indicators(
        adjusted, current_stock, lead_time_days, service_level, promo_start, promo_end
    )

    return {
        "baseline_forecast": baseline,
        "adjusted_forecast": adjusted,
        "uplift_percent": round(uplift_pct, 2),
        "uplift_source": uplift_source,
        "historical_promo_count": hist_count,
        "additional_units": additional_units,
        "current_stock": round(current_stock, 2),
        **stock,
        "rag_context": rag_context,
        "rag_sources": rag_sources,
        "rag_recommendations": rag_recommendations,
        "llm_confidence": llm_confidence,
        "is_synthetic_baseline": is_synthetic_baseline,
        "detected_event": event_type,
        "detected_event_label": EVENT_LABELS.get(event_type, None) if event_type else None,
        "similar_campaigns": similar_campaigns,
    }


# ─── scenario persistence ────────────────────────────────────────────────────

def save_scenario(
    db: Session,
    scenario_name: str | None,
    request_params: dict[str, Any],
    results: dict[str, Any],
    rag_explanation: str | None,
    rag_sources: list[str],
) -> int:
    _ensure_tables()
    compact = {k: v for k, v in results.items() if k not in ("baseline_forecast", "adjusted_forecast")}
    row = db.execute(
        text("""
            INSERT INTO whatif_scenarios (scenario_name, request_params, results, rag_explanation, rag_sources)
            VALUES (:name, :req::jsonb, :res::jsonb, :rag, :sources::jsonb)
            RETURNING id
        """),
        {
            "name": scenario_name or f"Simulation {request_params.get('service_type', '')} {request_params.get('promo_start', '')}",
            "req": json.dumps(request_params, default=str),
            "res": json.dumps(compact, default=str),
            "rag": rag_explanation,
            "sources": json.dumps(rag_sources),
        },
    ).mappings().one()
    scenario_id = int(row["id"])

    # Write the simulated scenario into fact_promotions so that future simulations
    # can find it via _query_historical_uplift (spec §A: historique campagnes similaires).
    # actual_uplift_percent is pre-filled with the simulated estimate; it can be updated
    # later via POST /record-outcome once the real campaign concludes.
    try:
        db.execute(
            text("""
                INSERT INTO fact_promotions
                    (service_type, region, channel, event_type,
                     discount_percent, promo_start, promo_end,
                     actual_uplift_percent, units_sold_during,
                     baseline_units_expected, notes)
                VALUES
                    (:service_type, :region, :channel, :event_type,
                     :discount_percent, :promo_start, :promo_end,
                     :actual_uplift_percent, :units_sold_during,
                     :baseline_units_expected, :notes)
            """),
            {
                "service_type":          request_params.get("service_type"),
                "region":                request_params.get("region"),
                "channel":               request_params.get("channel"),
                "event_type":            results.get("detected_event"),
                "discount_percent":      request_params.get("discount_percent"),
                "promo_start":           request_params.get("promo_start"),
                "promo_end":             request_params.get("promo_end"),
                "actual_uplift_percent": results.get("uplift_percent"),
                "units_sold_during":     results.get("total_promo_demand"),
                "baseline_units_expected": (
                    (results.get("total_promo_demand") or 0)
                    - (results.get("additional_units") or 0)
                ),
                "notes": f"Simulated — whatif_scenario_id={scenario_id}",
            },
        )
    except Exception as exc:
        logger.warning("Failed to mirror scenario into fact_promotions: %s", exc)

    db.commit()
    return scenario_id


def record_campaign_outcome(
    db: Session,
    service_type: str,
    promo_start: date,
    promo_end: date,
    discount_percent: float,
    actual_uplift_percent: float,
    region: str | None = None,
    channel: str | None = None,
    units_sold_during: float | None = None,
    baseline_units_expected: float | None = None,
    notes: str | None = None,
) -> int:
    """
    Insert a real observed campaign outcome into fact_promotions.

    Called from POST /record-outcome to record actual uplift after a campaign
    ends. This data is then found by _query_historical_uplift on future
    simulations, fulfilling spec §A: historique campagnes similaires.

    Returns the new fact_promotions row id.
    """
    _ensure_tables()
    event_type = detect_promo_event(promo_start, promo_end)
    row = db.execute(
        text("""
            INSERT INTO fact_promotions
                (service_type, region, channel, event_type,
                 discount_percent, promo_start, promo_end,
                 actual_uplift_percent, units_sold_during,
                 baseline_units_expected, notes)
            VALUES
                (:service_type, :region, :channel, :event_type,
                 :discount_percent, :promo_start, :promo_end,
                 :actual_uplift_percent, :units_sold_during,
                 :baseline_units_expected, :notes)
            RETURNING id
        """),
        {
            "service_type":          service_type,
            "region":                region,
            "channel":               channel,
            "event_type":            event_type,
            "discount_percent":      discount_percent,
            "promo_start":           promo_start,
            "promo_end":             promo_end,
            "actual_uplift_percent": actual_uplift_percent,
            "units_sold_during":     units_sold_during,
            "baseline_units_expected": baseline_units_expected,
            "notes":                 notes,
        },
    ).mappings().one()
    db.commit()
    return int(row["id"])


def list_scenarios(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    _ensure_tables()
    rows = db.execute(
        text("""
            SELECT id, scenario_name, request_params, results, rag_explanation, rag_sources, created_at
            FROM whatif_scenarios
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"limit": limit},
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "scenario_name": r["scenario_name"],
            "request_params": r["request_params"],
            "results": r["results"],
            "rag_explanation": r["rag_explanation"],
            "rag_sources": r["rag_sources"] or [],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


def compare_scenarios(db: Session, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    _ensure_tables()
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": v for i, v in enumerate(ids)}
    rows = db.execute(
        text(f"""
            SELECT id, scenario_name, request_params, results, created_at
            FROM whatif_scenarios
            WHERE id IN ({placeholders})
            ORDER BY created_at DESC
        """),
        params,
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "scenario_name": r["scenario_name"],
            "request_params": r["request_params"],
            "results": r["results"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
