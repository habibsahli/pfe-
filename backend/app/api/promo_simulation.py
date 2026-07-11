"""
What-If Promotion Simulation API

Endpoints:
  POST /api/simulation/promo/simulate  — run a simulation
  POST /api/simulation/promo/save      — persist a scenario
  GET  /api/simulation/promo/history   — list saved scenarios
  GET  /api/simulation/promo/compare   — compare multiple scenarios
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.promo_simulation_service import (
    compare_scenarios,
    list_scenarios,
    lookup_stock_snapshot,
    run_promo_simulation,
    save_scenario,
    record_campaign_outcome,
)

router = APIRouter(prefix="/api/simulation/promo", tags=["What-If Promo"])
logger = logging.getLogger(__name__)

VALID_SERVICE_TYPES = {"FIBRE", "5G", "DATA", "VOD"}
VALID_SERVICE_LEVELS = {0.90, 0.95, 0.98, 0.99}


# ─── request / response schemas ──────────────────────────────────────────────

VALID_CHANNELS = {"boutique", "online", "app", "call_center", "partenaire"}

VALID_EVENT_TYPES = {
    "ramadan", "eid_fitr", "eid_adha",
    "revolution", "nouvel_an", "fete_independance", "fete_nationale",
    "rentree_scolaire", "ete",
}


class PromoSimulationRequest(BaseModel):
    service_type: str = Field(..., description="FIBRE | 5G | DATA | VOD")
    region: str | None = Field(None, description="Governorate (optional, national if omitted)")
    channel: str | None = Field(None, description="boutique | online | app | call_center | partenaire (optional)")
    discount_percent: float = Field(..., ge=5.0, le=50.0, description="Discount percentage 5–50")
    promo_start: date
    promo_end: date
    current_stock: float | None = Field(None, ge=0, description="Current on-hand units — auto-fetched from fact_stock if omitted")
    lead_time_days: int = Field(7, ge=1, le=90, description="Supplier lead time in days")
    service_level: float = Field(0.95, description="Service level for safety stock (0.90–0.99)")
    skip_rag: bool = Field(False, description="Skip RAG enrichment for faster response")
    event_type_override: str | None = Field(
        None,
        description=(
            "Force a specific market event context instead of auto-detecting from dates. "
            "Use 'none' to explicitly simulate a standard promo with no event. "
            f"Valid values: {sorted(VALID_EVENT_TYPES)} | 'none' | null (auto-detect)"
        ),
    )

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str | None) -> str | None:
        if v is None:
            return None
        lower = v.strip().lower()
        if lower not in VALID_CHANNELS:
            raise ValueError(
                f"channel must be one of {sorted(VALID_CHANNELS)} (got '{v}')"
            )
        return lower

    @field_validator("event_type_override")
    @classmethod
    def validate_event_override(cls, v: str | None) -> str | None:
        if v is None or v.strip().lower() in ("", "auto"):
            return None  # null → auto-detect from dates
        lower = v.strip().lower()
        if lower == "none":
            return "none"  # sentinel: no event, standard promo
        if lower not in VALID_EVENT_TYPES:
            raise ValueError(f"event_type_override must be one of {sorted(VALID_EVENT_TYPES)} | 'none' | null")
        return lower

    @field_validator("service_type")
    @classmethod
    def validate_service(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in VALID_SERVICE_TYPES:
            raise ValueError(f"service_type must be one of {sorted(VALID_SERVICE_TYPES)}")
        return upper

    @field_validator("service_level")
    @classmethod
    def validate_service_level(cls, v: float) -> float:
        nearest = min(VALID_SERVICE_LEVELS, key=lambda x: abs(x - v))
        return nearest

    @field_validator("promo_end")
    @classmethod
    def validate_dates(cls, v: date, info: Any) -> date:
        start = info.data.get("promo_start")
        if start and v <= start:
            raise ValueError("promo_end must be after promo_start")
        if start and (v - start).days > 180:
            raise ValueError("Promo duration cannot exceed 180 days")
        return v


class SaveScenarioRequest(BaseModel):
    scenario_name: str | None = None
    request_params: dict[str, Any]
    results: dict[str, Any]
    rag_explanation: str | None = None
    rag_sources: list[str] = []


class RecordOutcomeRequest(BaseModel):
    """Record the actual observed outcome of a past promotion campaign.

    Updating fact_promotions with real uplift data improves future simulations
    that find this campaign via the historical lookup (spec §A).
    """
    service_type: str = Field(..., description="FIBRE | 5G | DATA | VOD")
    region: str | None = None
    channel: str | None = None
    promo_start: date
    promo_end: date
    discount_percent: float = Field(..., ge=0, le=100)
    actual_uplift_percent: float = Field(..., ge=0, le=500, description="Observed uplift %")
    units_sold_during: float | None = Field(None, ge=0)
    baseline_units_expected: float | None = Field(None, ge=0)
    notes: str | None = None


# ─── endpoints ───────────────────────────────────────────────────────────────

@router.get("/stock-snapshot")
async def get_stock_snapshot(
    service_type: str = Query(..., description="FIBRE | 5G | DATA | VOD"),
    region: str | None = Query(None, description="Governorate name (optional)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Return the latest available stock level for a service + region from fact_stock.
    Used by the frontend to pre-fill the current_stock field automatically.
    """
    try:
        upper = service_type.strip().upper()
        if upper not in VALID_SERVICE_TYPES:
            raise HTTPException(status_code=422, detail=f"service_type must be one of {sorted(VALID_SERVICE_TYPES)}")
        snapshot = lookup_stock_snapshot(db=db, service_type=upper, region=region or None)
        return {"status": "success", **snapshot}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Stock snapshot lookup failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/simulate")
async def simulate_promo(
    req: PromoSimulationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Run a what-if promotion simulation.

    Returns baseline + adjusted forecast, uplift estimate, stock indicators,
    rupture risk classification, and RAG-powered campaign insights.
    If current_stock is omitted, it is auto-fetched from fact_stock.
    """
    try:
        # Auto-fetch stock when not provided by the caller
        stock_source = "manual"
        current_stock = req.current_stock
        if current_stock is None:
            snapshot = lookup_stock_snapshot(db=db, service_type=req.service_type, region=req.region)
            current_stock = snapshot["available_stock"]
            stock_source = snapshot["source"]
            logger.info(
                "Auto-fetched current_stock=%.0f for %s/%s from %s",
                current_stock, req.service_type, req.region or "national", stock_source,
            )

        result = run_promo_simulation(
            db=db,
            service_type=req.service_type,
            discount_percent=req.discount_percent,
            promo_start=req.promo_start,
            promo_end=req.promo_end,
            current_stock=current_stock,
            lead_time_days=req.lead_time_days,
            service_level=req.service_level,
            skip_rag=req.skip_rag,
            region=req.region,
            channel=req.channel,
            event_type_override=req.event_type_override,
        )

        # Warn when the caller explicitly passed stock=0, which may mean "unknown"
        # rather than a genuinely empty warehouse. Auto-fetch (current_stock=null)
        # is always more reliable than a manual zero.
        stock_zero_warning: str | None = None
        if current_stock == 0.0 and stock_source == "manual":
            stock_zero_warning = (
                "current_stock was provided as 0. If stock is actually unknown, "
                "omit current_stock so it is auto-fetched from fact_stock. "
                "A genuine zero-stock scenario will produce CRITICAL rupture risk."
            )

        return {
            "status": "success",
            "stock_source": stock_source,
            "stock_zero_warning": stock_zero_warning,
            **result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Promo simulation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/save")
async def save_promo_scenario(
    req: SaveScenarioRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Persist a simulation scenario for later comparison."""
    try:
        scenario_id = save_scenario(
            db=db,
            scenario_name=req.scenario_name,
            request_params=req.request_params,
            results=req.results,
            rag_explanation=req.rag_explanation,
            rag_sources=req.rag_sources,
        )
        return {"status": "saved", "scenario_id": scenario_id}
    except Exception as exc:
        logger.error("Save scenario failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/history")
async def get_scenario_history(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List saved what-if scenarios, most recent first."""
    try:
        scenarios = list_scenarios(db=db, limit=limit)
        return {"status": "success", "count": len(scenarios), "scenarios": scenarios}
    except Exception as exc:
        logger.error("List scenarios failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/record-outcome")
async def record_promo_outcome(
    req: RecordOutcomeRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Record the actual observed outcome of a completed promotion campaign.

    Writes into fact_promotions so future simulations for the same service /
    region / discount range can find this as a real historical data point
    (spec §A: historique campagnes similaires).
    """
    try:
        upper = req.service_type.strip().upper()
        if upper not in VALID_SERVICE_TYPES:
            raise HTTPException(status_code=422, detail=f"service_type must be one of {sorted(VALID_SERVICE_TYPES)}")
        record_id = record_campaign_outcome(
            db=db,
            service_type=upper,
            region=req.region,
            channel=req.channel,
            promo_start=req.promo_start,
            promo_end=req.promo_end,
            discount_percent=req.discount_percent,
            actual_uplift_percent=req.actual_uplift_percent,
            units_sold_during=req.units_sold_during,
            baseline_units_expected=req.baseline_units_expected,
            notes=req.notes,
        )
        return {"status": "recorded", "fact_promotions_id": record_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Record outcome failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/compare")
async def compare_promo_scenarios(
    ids: str = Query(..., description="Comma-separated scenario IDs, e.g. 1,2,3"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compare 2-4 saved scenarios side by side."""
    try:
        id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
        if not id_list:
            raise HTTPException(status_code=422, detail="Provide at least one scenario ID")
        if len(id_list) > 4:
            raise HTTPException(status_code=422, detail="Compare at most 4 scenarios at once")
        scenarios = compare_scenarios(db=db, ids=id_list)
        return {"status": "success", "count": len(scenarios), "scenarios": scenarios}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=422, detail="IDs must be integers")
    except Exception as exc:
        logger.error("Compare scenarios failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
