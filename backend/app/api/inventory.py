"""
Stock forecasting API routes — parallel to sales forecasting endpoints.
Trains inventory models and generates 6-month forecasts aggregated by PRODUCT_FAMILY.
Includes stock recommendation engine for inventory optimization.
"""
from __future__ import annotations

import json
import uuid
import logging
import threading
from typing import Optional, List, Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db, SessionLocal
from app.core.state import session_manager, TrainingStatus
from app.services.inventory_forecasting_service import (
    train_inventory_models,
    generate_inventory_forecast,
    resolve_inventory_models,
    _select_forecast_model,
)
from app.services.stock_recommendation_service import (
    StockRecommendationEngine,
    RecommendationInput,
    ForecastPoint,
    RecommendationResponse,
    Z_SCORE_BY_SERVICE_LEVEL,
    ensure_sku_thresholds_table,
    upsert_sku_threshold,
    list_sku_thresholds,
    get_sku_threshold,
    delete_sku_threshold,
    load_thresholds_for_products,
    fetch_demand_segments,
)
from app.services.rag_stock_recommendation_service import generate_recommendations_with_rag
from app.core.tracing import get_tracer, flush_tracing
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)
_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value

router = APIRouter(prefix="/api/inventory", tags=["inventory"])

# ── Async forecast job store ──────────────────────────────────────────────────
# Keyed by forecast_job_id; entries are small (status + result dict or error str).
_forecast_jobs: Dict[str, Dict[str, Any]] = {}
_forecast_jobs_lock = threading.Lock()

# ── Async RAG recommendation job store ───────────────────────────────────────
_rag_jobs: Dict[str, Dict[str, Any]] = {}
_rag_jobs_lock = threading.Lock()


def _run_background_forecast(
    job_id: str,
    model: str,
    horizon: int,
    granularity: str,
    scope: str,
    family: Optional[str],
    forecast_target: str,
    forecast_scope: str,
    service_type: Optional[str],
    session_id: str,
    cache_key: str,
) -> None:
    db = SessionLocal()
    try:
        result = generate_inventory_forecast(
            db=db,
            best_model_name=model,
            horizon=horizon,
            granularity=granularity,
            scope=scope,
            family=family,
            forecast_target=forecast_target,
            forecast_scope=forecast_scope,
            service_type=service_type,
        )
        session_manager.cache_forecast(cache_key, result, session_id=session_id, db=db)
        final = {
            "session_id": session_id,
            "model_used": model,
            "status": "success",
            **result,
        }
        with _forecast_jobs_lock:
            _forecast_jobs[job_id] = {"status": "completed", "result": final, "error": None}
        logger.info("✓ Async inventory forecast done: job=%s model=%s", job_id, model)
    except Exception as exc:
        logger.error("Async inventory forecast failed: job=%s %s", job_id, exc, exc_info=True)
        with _forecast_jobs_lock:
            _forecast_jobs[job_id] = {"status": "failed", "result": None, "error": str(exc)}
    finally:
        db.close()


def _run_background_rag(
    job_id: str,
    inputs: list,
    z_score: float,
    service_type: Optional[str],
    top_k: int,
    session_id: str,
) -> None:
    from openinference.semconv.trace import SpanAttributes as _SA
    with _tracer.start_as_current_span("RAG Stock Recommendation Pipeline") as root:
        root.set_attribute(_KIND, _CHAIN)
        root.set_attribute("rag.job_id", job_id)
        root.set_attribute("rag.session_id", session_id)
        root.set_attribute("rag.product_count", len(inputs))
        root.set_attribute("rag.top_k", top_k)
        root.set_attribute("rag.service_type", service_type or "ALL")
        root.set_attribute("rag.llm_model", settings.OLLAMA_LLM_MODEL)
        root.set_attribute(_SA.INPUT_VALUE, json.dumps({
            "job_id": job_id,
            "session_id": session_id,
            "products": [inp.product_name for inp in inputs],
            "z_score": z_score,
            "top_k": top_k,
            "service_type": service_type,
        }))
        root.set_attribute(_SA.INPUT_MIME_TYPE, "application/json")
        try:
            result = generate_recommendations_with_rag(
                inputs=inputs,
                z_score=z_score,
                service_type=service_type,
                top_k_per_product=top_k,
            )
            final = {
                "session_id": session_id,
                "status": "success",
                **result,
                "metadata": {
                    **result["metadata"],
                    "z_score_used": z_score,
                },
            }
            summary = result.get("summary", {})
            root.set_attribute(_SA.OUTPUT_VALUE, json.dumps({
                "products_enriched": len(result.get("recommendations", [])),
                "critical_rupture_count": summary.get("critical_rupture_count", 0),
                "total_qty_to_order": summary.get("total_qty_to_order", 0),
            }))
            root.set_attribute(_SA.OUTPUT_MIME_TYPE, "application/json")
            with _rag_jobs_lock:
                _rag_jobs[job_id] = {"status": "completed", "result": final, "error": None}
            logger.info("✓ Async RAG recommendations done: job=%s products=%s", job_id, len(inputs))
        except Exception as exc:
            logger.error("Async RAG recommendations failed: job=%s %s", job_id, exc, exc_info=True)
            root.set_attribute("error", True)
            root.set_attribute("error.message", str(exc))
            with _rag_jobs_lock:
                _rag_jobs[job_id] = {"status": "failed", "result": None, "error": str(exc)}
    # Drain queued spans so the full pipeline trace is visible in Phoenix as soon
    # as the job finishes (the batch processor would otherwise flush on its own delay).
    flush_tracing()


def _resolve_z_score(service_level: float) -> float:
    """Return the z-score for a given service level, or raise HTTP 422 if unsupported."""
    for sl, z in Z_SCORE_BY_SERVICE_LEVEL.items():
        if abs(service_level - sl) < 0.001:
            return z
    supported = sorted(Z_SCORE_BY_SERVICE_LEVEL)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Unsupported service_level {service_level}. Must be one of {supported}.",
    )


def _validate_recommendation_input(inp_req: Any) -> None:
    """Raise HTTP 422 for inputs that would produce nonsensical recommendations."""
    pid = inp_req.product_id
    if inp_req.current_stock < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Product '{pid}': current_stock must be >= 0 (got {inp_req.current_stock}).",
        )
    if inp_req.avg_monthly_demand <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Product '{pid}': avg_monthly_demand must be > 0 (got {inp_req.avg_monthly_demand}).",
        )
    if len(inp_req.forecast_series) < 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Product '{pid}': forecast_series must have at least 3 points for trend analysis (got {len(inp_req.forecast_series)}).",
        )


class InventoryTrainingRequest(BaseModel):
    """Request to train inventory forecasting models."""

    session_id: str = Field(
        ..., description="Session ID from data upload"
    )
    horizon: int = Field(
        6, ge=1, description="Forecast horizon in periods (monthly up to 12, daily up to 365)"
    )
    models: List[str] = Field(
        ["all"], description="Models to train: 'all', or specific model names"
    )
    enable_generative: bool = Field(
        True, description="Enable generative models (chronos, timesfm)"
    )
    granularity: str = Field(
        "monthly",
        pattern="^(monthly|daily)$",
        description="Temporal granularity",
    )
    forecast_target: str = Field(
        "stock",
        pattern="^(stock|demand)$",
        description="Forecast target: stock or demand",
    )
    forecast_scope: str = Field(
        "national",
        pattern="^(national|by_product_type|by_governorate)$",
        description="Forecast scope: national, by_product_type, or by_governorate",
    )
    service_type: Optional[str] = Field(
        None,
        description="Filter to a single service: FIBRE, 5G, DATA_BUNDLE, VOD. Omit to train on all services.",
    )

    @field_validator("service_type")
    @classmethod
    def validate_service_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"FIBRE", "5G", "DATA_BUNDLE", "VOD"}
        upper = v.strip().upper()
        if upper not in allowed:
            raise ValueError(f"service_type must be one of {sorted(allowed)}, got '{v}'")
        return upper

    @model_validator(mode="after")
    def validate_horizon_by_granularity(self):
        max_horizon = 12 if self.granularity == "monthly" else 365
        if self.horizon > max_horizon:
            raise ValueError(
                f"horizon must be between 1 and {max_horizon} for {self.granularity} granularity"
            )
        return self


class InventoryForecastRequest(BaseModel):
    """Request to generate inventory forecast from trained model."""

    session_id: str = Field(
        ..., description="Session ID from training job"
    )
    model: str = Field(
        ..., description="Model name to use (from training results)"
    )
    horizon: int = Field(
        6, ge=1, description="Forecast horizon in periods (monthly up to 12, daily up to 365)"
    )
    granularity: str = Field(
        "monthly",
        pattern="^(monthly|daily)$",
        description="Temporal granularity",
    )
    scope: str = Field(
        "global",
        pattern="^(global|per_family|both)$",
        description="Forecast scope: 'global', 'per_family' (single or all), or 'both'",
    )
    family: Optional[str] = Field(
        None,
        description="Optional product family to forecast when scope='per_family' (if omitted, all families returned)",
    )
    forecast_target: str = Field(
        "stock",
        pattern="^(stock|demand)$",
        description="Forecast target: stock or demand",
    )
    forecast_scope: str = Field(
        "national",
        pattern="^(national|by_product_type|by_governorate)$",
        description="Forecast scope: national, by_product_type, or by_governorate",
    )
    service_type: Optional[str] = Field(
        None,
        description="Filter to a single service: FIBRE, 5G, DATA_BUNDLE, VOD.",
    )

    @field_validator("service_type")
    @classmethod
    def validate_service_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"FIBRE", "5G", "DATA_BUNDLE", "VOD"}
        upper = v.strip().upper()
        if upper not in allowed:
            raise ValueError(f"service_type must be one of {sorted(allowed)}, got '{v}'")
        return upper

    @model_validator(mode="after")
    def validate_horizon_by_granularity(self):
        max_horizon = 12 if self.granularity == "monthly" else 365
        if self.horizon > max_horizon:
            raise ValueError(
                f"horizon must be between 1 and {max_horizon} for {self.granularity} granularity"
            )
        return self


def _run_background_training(
    training_id: str,
    session_id: str,
    horizon: int,
    enable_generative: bool,
    selected_models: List[str],
    granularity: str,
    forecast_target: str,
    forecast_scope: str,
    service_type: Optional[str] = None,
) -> None:
    """Execute inventory training in a background thread with its own DB session.

    The request-scoped session is closed before this runs, so a fresh session
    is opened here and closed in the finally block regardless of outcome.
    """
    db = SessionLocal()
    try:
        session_manager.update_training_job(
            training_id, db=db, status=TrainingStatus.RUNNING, progress_pct=5
        )
        results = train_inventory_models(
            db=db,
            horizon=horizon,
            enable_generative=enable_generative,
            selected_models=selected_models,
            granularity=granularity,
            forecast_target=forecast_target,
            forecast_scope=forecast_scope,
            service_type=service_type,
        )
        best_metric_model = results[0]["model"] if results else None
        best_model = _select_forecast_model(results) or best_metric_model
        session_manager.update_training_job(
            training_id=training_id,
            db=db,
            status=TrainingStatus.COMPLETED,
            progress_pct=100,
            models_completed=len(results),
            results=results,
            best_model=best_model,
        )
        logger.info("✓ Inventory training completed: %s models, best=%s", len(results), best_model)
    except Exception as exc:
        session_manager.update_training_job(
            training_id=training_id,
            db=db,
            status=TrainingStatus.FAILED,
            error_message=str(exc),
        )
        logger.error("Inventory background training failed: %s", exc, exc_info=True)
    finally:
        db.close()


@router.post("/training", status_code=status.HTTP_202_ACCEPTED)
async def start_inventory_training(
    request: InventoryTrainingRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Dict:
    """
    Train inventory forecasting models on historical stock data.

    Returns 202 Accepted immediately with a training_id. Poll
    GET /api/inventory/training/{training_id} for status and results.
    Training runs in the background — large datasets no longer cause HTTP timeouts.
    """
    session = session_manager.get_session(request.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {request.session_id} not found",
        )

    # Size the progress bar off the models that will actually run: only installed
    # generative models are counted, and a non-'all' selection is honored. Using a
    # hardcoded 8 previously left the bar stuck at e.g. 6/8 when chronos/timesfm
    # weren't installed.
    planned_models = resolve_inventory_models(request.models, request.enable_generative)
    training_id = session_manager.create_training_job(
        session_id=request.session_id,
        total_models=len(planned_models),
        db=db,
    )

    background_tasks.add_task(
        _run_background_training,
        training_id=training_id,
        session_id=request.session_id,
        horizon=request.horizon,
        enable_generative=request.enable_generative,
        selected_models=request.models,
        granularity=request.granularity,
        forecast_target=request.forecast_target,
        forecast_scope=request.forecast_scope,
        service_type=request.service_type,
    )

    logger.info(
        "Inventory training queued: training_id=%s session=%s",
        training_id, request.session_id,
    )
    return {
        "training_id": training_id,
        "session_id": request.session_id,
        "status": "running",
        "message": (
            f"Training started in the background. "
            f"Poll GET /api/inventory/training/{training_id} for status and results."
        ),
    }


@router.post("/forecast", status_code=status.HTTP_202_ACCEPTED)
async def generate_inventory_forecast_endpoint(
    request: InventoryForecastRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """
    Start a forecast generation job in the background.
    Returns 202 immediately with a forecast_job_id.
    Poll GET /api/inventory/forecast/status/{forecast_job_id} for progress and results.
    """
    session = session_manager.get_session(request.session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {request.session_id} not found",
        )

    # horizon and granularity MUST be part of the key: they change the shape of the
    # result (number of points and monthly vs daily frequency), so omitting them would
    # serve e.g. a 6-point monthly forecast in response to a 12-point daily request.
    cache_key = (
        f"{request.session_id}:inventory:{request.model}:"
        f"scope={request.scope}:family={request.family or 'all'}:"
        f"target={request.forecast_target}:forecast_scope={request.forecast_scope}:"
        f"service={request.service_type or 'all'}:"
        f"horizon={request.horizon}:granularity={request.granularity}"
    )

    # Return cached result immediately if available
    cached = session_manager.get_cached_forecast(cache_key)
    if cached:
        logger.info("Returning cached inventory forecast for %s", request.session_id)
        job_id = str(uuid.uuid4())
        with _forecast_jobs_lock:
            _forecast_jobs[job_id] = {"status": "completed", "result": cached, "error": None}
        return {"forecast_job_id": job_id, "status": "completed", "cached": True}

    job_id = str(uuid.uuid4())
    with _forecast_jobs_lock:
        _forecast_jobs[job_id] = {"status": "running", "result": None, "error": None}

    background_tasks.add_task(
        _run_background_forecast,
        job_id=job_id,
        model=request.model,
        horizon=request.horizon,
        granularity=request.granularity,
        scope=request.scope,
        family=request.family,
        forecast_target=request.forecast_target,
        forecast_scope=request.forecast_scope,
        service_type=request.service_type,
        session_id=request.session_id,
        cache_key=cache_key,
    )

    logger.info("Inventory forecast job queued: job=%s model=%s session=%s", job_id, request.model, request.session_id)
    return {"forecast_job_id": job_id, "status": "running", "cached": False}


@router.get("/forecast/status/{forecast_job_id}")
async def get_inventory_forecast_status(forecast_job_id: str) -> dict:
    """Poll for forecast job completion. Returns status and result when done."""
    with _forecast_jobs_lock:
        job = _forecast_jobs.get(forecast_job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Forecast job {forecast_job_id} not found",
        )
    return {
        "forecast_job_id": forecast_job_id,
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
    }


@router.get("/training/{training_id}")
async def get_inventory_training_status(training_id: str) -> Dict:
    """Get status of an inventory training job."""
    job = session_manager.get_training_job(training_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Training job {training_id} not found",
        )
    return {
        "training_id": job.training_id,
        "session_id": job.session_id,
        "status": job.status,
        "progress": job.progress_pct,
        "models_completed": job.models_completed,
        "total_models": job.total_models,
        "results": job.results,
        "best_model": job.best_model,
        "error": job.error_message,
    }


# ============================================================================
# Stock Recommendation Endpoint Models
# ============================================================================


class ForecastPointRequest(BaseModel):
    """Single forecast data point in recommendation request."""

    date: str = Field(..., description="Forecast date (ISO format)")
    value: float = Field(..., ge=0.0, description="Forecasted quantity")
    lower_bound: Optional[float] = Field(None, description="Confidence interval lower bound")
    upper_bound: Optional[float] = Field(None, description="Confidence interval upper bound")


class RecommendationInputRequest(BaseModel):
    """Input for stock recommendation calculation."""

    product_id: str = Field(..., description="Product identifier")
    product_name: str = Field(..., description="Product name")
    product_type: str = Field(
        ...,
        pattern="^(SUBSCRIPTION|CPE_HARDWARE|SMARTPHONE_HW|UNKNOWN)$",
        description="Product type: SUBSCRIPTION | CPE_HARDWARE | SMARTPHONE_HW",
    )
    governorate: str = Field(..., description="Governorate or 'NATIONAL'")
    current_stock: int = Field(..., ge=0, description="Current on-hand stock")
    forecast_series: List[ForecastPointRequest] = Field(..., description="6-month forecast points")
    avg_monthly_demand: float = Field(
        ..., ge=0, description="Average monthly demand (QTE_VTE + ACTIVATIONS)"
    )
    lead_time_months: Optional[float] = Field(
        None, description="Lead time override (months); uses defaults if omitted"
    )
    min_order_qty: Optional[int] = Field(
        None, description="Min order qty override; uses defaults if omitted"
    )
    stock_min_threshold: Optional[int] = Field(
        None, ge=0, description="Custom minimum stock floor — overrides algorithmic safety stock if higher"
    )
    stock_max_capacity: Optional[int] = Field(
        None, ge=0, description="Warehouse max capacity — caps order quantity to avoid over-stocking"
    )
    data_source_mix: Optional[Dict[str, float]] = Field(
        None, description="Data source composition: {'REAL': N, 'SIMULATED': M}"
    )


class StockRecommendationRequest(BaseModel):
    """Request for batch stock recommendations."""

    session_id: str = Field(..., description="Forecast session ID for traceability")
    recommendations_input: List[RecommendationInputRequest] = Field(
        ..., description="List of products to generate recommendations for"
    )
    service_level: float = Field(
        0.95,
        ge=0.85,
        le=0.99,
        description="Service level (0.85 - 0.99); default 0.95 = z-score 1.65",
    )
    lead_time_overrides: Optional[Dict[str, float]] = Field(
        None, description="Override lead times by product_id"
    )
    min_order_qty_overrides: Optional[Dict[str, int]] = Field(
        None, description="Override min order quantities by product_id"
    )


class StockRecommendationRagRequest(StockRecommendationRequest):
    """Request for RAG-augmented batch stock recommendations (Variant A)."""

    service_type: Optional[str] = Field(
        None,
        description="Filter retrieved policy chunks by service type (e.g. FIBRE, 5G). "
                    "Omit to search across all indexed documents.",
    )
    rag_top_k: int = Field(
        4,
        ge=1,
        le=10,
        description="Number of policy/rule chunks to retrieve per product from the vector store.",
    )


class SkuThresholdRequest(BaseModel):
    """Upsert a per-SKU stock threshold."""
    product_id: str = Field(..., description="Product identifier (matches product_id in recommendations)")
    governorate: str = Field("NATIONAL", description="Governorate or 'NATIONAL' for a global threshold")
    min_stock: Optional[int] = Field(None, ge=0, description="Absolute minimum stock floor (units)")
    max_stock: Optional[int] = Field(None, ge=0, description="Maximum storage capacity (units)")
    notes: Optional[str] = Field(None, description="Free-text rationale for this threshold")


recommendation_router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])


@recommendation_router.get("/demand-stats")
async def get_demand_stats(
    forecast_scope: str = Query("national", pattern="^(national|by_product_type|by_governorate)$"),
    months: int = Query(3, ge=1, le=12, description="Rolling window in months for avg demand"),
    db: Session = Depends(get_db),
) -> dict:
    """Return rolling N-month average demand (QTE_VTE + ACTIVATIONS) per product segment.

    Uses the two-source demand formula from the spec: sales_qty (QTE_VTE) +
    activations_qty (ACTIVATIONS). This matches the _inventory_target_expression
    used in the training pipeline for the 'demand' forecast target.

    The query lives in stock_recommendation_service.fetch_demand_segments so the
    agent's get_demand_statistics tool reuses the exact same demand definition.
    """
    segments = fetch_demand_segments(db, forecast_scope=forecast_scope, months=months)
    return {"segments": segments, "months_window": months, "forecast_scope": forecast_scope}


# ── Per-SKU threshold configuration endpoints (UC7) ──────────────────────────

@recommendation_router.put("/thresholds")
async def upsert_threshold(req: SkuThresholdRequest) -> dict:
    """
    Create or update a custom stock threshold for a (product_id, governorate) pair.

    min_stock overrides the algorithmic safety stock when higher.
    max_stock caps the recommended order quantity so the warehouse is never over-filled.
    Thresholds are auto-applied to future /recommendations calls for the same product.
    """
    try:
        row = upsert_sku_threshold(
            product_id=req.product_id,
            governorate=req.governorate,
            min_stock=req.min_stock,
            max_stock=req.max_stock,
            notes=req.notes,
        )
        return {"status": "saved", "threshold": row}
    except Exception as exc:
        logger.error("Upsert threshold failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@recommendation_router.get("/thresholds")
async def get_thresholds(product_id: Optional[str] = None) -> dict:
    """List all configured SKU thresholds, optionally filtered by product_id."""
    try:
        rows = list_sku_thresholds(product_id=product_id)
        return {"thresholds": rows, "count": len(rows)}
    except Exception as exc:
        logger.error("List thresholds failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@recommendation_router.get("/thresholds/{product_id}")
async def get_threshold(product_id: str, governorate: str = "NATIONAL") -> dict:
    """Get the threshold for a specific (product_id, governorate) pair."""
    row = get_sku_threshold(product_id=product_id, governorate=governorate)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No threshold configured for product '{product_id}' / '{governorate}'",
        )
    return row


@recommendation_router.delete("/thresholds/{product_id}")
async def delete_threshold(product_id: str, governorate: str = "NATIONAL") -> dict:
    """Remove a custom threshold. The recommendation engine reverts to algorithmic values."""
    deleted = delete_sku_threshold(product_id=product_id, governorate=governorate)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"No threshold found for product '{product_id}' / '{governorate}'",
        )
    return {"status": "deleted", "product_id": product_id, "governorate": governorate}


@recommendation_router.post("/recommendations")
async def generate_stock_recommendations(
    request: StockRecommendationRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Generate stock recommendations from forecast data.

    Orchestrates validation, conversion to the pure recommendation service input,
    and returns actionable stock control guidance.
    """
    try:
        logger.info(
            "Starting stock recommendations: session=%s, products=%s",
            request.session_id,
            len(request.recommendations_input),
        )

        if request.session_id:
            session = session_manager.get_session(request.session_id)
            if not session:
                logger.warning("Session %s not found (continuing anyway)", request.session_id)

        z_score = _resolve_z_score(request.service_level)

        # Bulk-load persisted per-SKU thresholds so callers don't need to supply
        # them on every request. Request-level values take precedence over DB values.
        threshold_map = load_thresholds_for_products(
            [(r.product_id, r.governorate) for r in request.recommendations_input]
        )

        inputs = []
        for inp_req in request.recommendations_input:
            _validate_recommendation_input(inp_req)
            forecast_series = [
                ForecastPoint(
                    date=fp.date,
                    value=fp.value,
                    lower_bound=fp.lower_bound,
                    upper_bound=fp.upper_bound,
                )
                for fp in inp_req.forecast_series
            ]

            lead_time = inp_req.lead_time_months
            if lead_time is None and request.lead_time_overrides:
                lead_time = request.lead_time_overrides.get(inp_req.product_id)

            min_order_qty = inp_req.min_order_qty
            if min_order_qty is None and request.min_order_qty_overrides:
                min_order_qty = request.min_order_qty_overrides.get(inp_req.product_id)

            db_thresh = threshold_map.get((inp_req.product_id, inp_req.governorate), {})
            min_stock = inp_req.stock_min_threshold if inp_req.stock_min_threshold is not None else db_thresh.get("min_stock")
            max_stock = inp_req.stock_max_capacity if inp_req.stock_max_capacity is not None else db_thresh.get("max_stock")

            inputs.append(
                RecommendationInput(
                    product_id=inp_req.product_id,
                    product_name=inp_req.product_name,
                    product_type=inp_req.product_type,
                    governorate=inp_req.governorate,
                    current_stock=inp_req.current_stock,
                    forecast_series=forecast_series,
                    avg_monthly_demand=inp_req.avg_monthly_demand,
                    lead_time_months=lead_time,
                    min_order_qty=min_order_qty,
                    stock_min_threshold=min_stock,
                    stock_max_capacity=max_stock,
                    data_source_mix=inp_req.data_source_mix,
                )
            )

        response: RecommendationResponse = StockRecommendationEngine.generate_recommendations(
            inputs=inputs,
            z_score=z_score,
        )

        logger.info(
            "✓ Stock recommendations generated: %s products, critical=%s",
            len(response.recommendations),
            response.summary["critical_rupture_count"],
        )

        return {
            "session_id": request.session_id,
            "status": "success",
            "recommendations": [vars(rec) for rec in response.recommendations],
            "summary": response.summary,
            "metadata": {
                **response.metadata,
                "service_level_input": request.service_level,
                "z_score_used": z_score,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Recommendation generation failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Recommendation generation failed: {str(exc)}",
        )


@recommendation_router.post("/recommendations/rag", status_code=status.HTTP_202_ACCEPTED)
async def generate_stock_recommendations_rag(
    request: StockRecommendationRagRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Variant A — RAG-augmented stock recommendations (async).

    Returns 202 immediately with a rag_job_id.
    Poll GET /api/v1/inventory/recommendations/rag/status/{rag_job_id} for results.
    LLM generation runs in the background to avoid browser/proxy timeout on slow models.
    """
    try:
        logger.info(
            "Starting RAG stock recommendations: session=%s, products=%s, rag_top_k=%s",
            request.session_id,
            len(request.recommendations_input),
            request.rag_top_k,
        )

        if request.session_id:
            session = session_manager.get_session(request.session_id)
            if not session:
                logger.warning("Session %s not found (continuing anyway)", request.session_id)

        z_score = _resolve_z_score(request.service_level)

        threshold_map = load_thresholds_for_products(
            [(r.product_id, r.governorate) for r in request.recommendations_input]
        )

        inputs = []
        for inp_req in request.recommendations_input:
            _validate_recommendation_input(inp_req)
            forecast_series = [
                ForecastPoint(
                    date=fp.date,
                    value=fp.value,
                    lower_bound=fp.lower_bound,
                    upper_bound=fp.upper_bound,
                )
                for fp in inp_req.forecast_series
            ]

            lead_time = inp_req.lead_time_months
            if lead_time is None and request.lead_time_overrides:
                lead_time = request.lead_time_overrides.get(inp_req.product_id)

            min_order_qty = inp_req.min_order_qty
            if min_order_qty is None and request.min_order_qty_overrides:
                min_order_qty = request.min_order_qty_overrides.get(inp_req.product_id)

            db_thresh = threshold_map.get((inp_req.product_id, inp_req.governorate), {})
            min_stock = inp_req.stock_min_threshold if inp_req.stock_min_threshold is not None else db_thresh.get("min_stock")
            max_stock = inp_req.stock_max_capacity if inp_req.stock_max_capacity is not None else db_thresh.get("max_stock")

            inputs.append(
                RecommendationInput(
                    product_id=inp_req.product_id,
                    product_name=inp_req.product_name,
                    product_type=inp_req.product_type,
                    governorate=inp_req.governorate,
                    current_stock=inp_req.current_stock,
                    forecast_series=forecast_series,
                    avg_monthly_demand=inp_req.avg_monthly_demand,
                    lead_time_months=lead_time,
                    min_order_qty=min_order_qty,
                    stock_min_threshold=min_stock,
                    stock_max_capacity=max_stock,
                    data_source_mix=inp_req.data_source_mix,
                )
            )

        job_id = str(uuid.uuid4())
        with _rag_jobs_lock:
            _rag_jobs[job_id] = {"status": "running", "result": None, "error": None}

        background_tasks.add_task(
            _run_background_rag,
            job_id=job_id,
            inputs=inputs,
            z_score=z_score,
            service_type=request.service_type,
            top_k=request.rag_top_k,
            session_id=request.session_id,
        )

        logger.info("RAG recommendation job queued: job=%s session=%s", job_id, request.session_id)
        return {"rag_job_id": job_id, "status": "running"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("RAG recommendation setup failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"RAG recommendation setup failed: {str(exc)}",
        )


@recommendation_router.get("/recommendations/rag/status/{rag_job_id}")
async def get_rag_recommendation_status(rag_job_id: str) -> dict:
    """Poll for RAG recommendation job completion."""
    with _rag_jobs_lock:
        job = _rag_jobs.get(rag_job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RAG job {rag_job_id} not found",
        )
    return {
        "rag_job_id": rag_job_id,
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
    }
