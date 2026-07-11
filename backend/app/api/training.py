"""
Model training endpoints
"""
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional, Literal
import logging
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.state import session_manager, TrainingStatus
from app.db.session import get_db
from app.services.forecasting_service import CLASSIC_MODELS, GENERATIVE_MODELS, list_target_values, resolve_target_value, resolve_service_code, train_models

router = APIRouter()
logger = logging.getLogger(__name__)


class TrainingRequest(BaseModel):
    """Training job request"""
    session_id: str
    horizon: int = 30
    models: List[str] = ["all"]
    enable_generative: bool = True
    granularity: Literal["daily", "monthly"] = "daily"
    target_level: Literal["service", "product", "category", "region"] = "service"
    target_value: Optional[str] = None
    # Which service to forecast (FIBRE/5G/DATA_BUNDLE/VOD, or 'ALL'/None for all).
    # Derived from the data, not the session — overrides the session's detected service.
    service_type: Optional[str] = None
    include_promotions: bool = True
    include_price: bool = True
    include_calendar: bool = True


@router.get("/sessions")
async def list_sessions():
    """List all available upload sessions"""
    sessions = session_manager.list_sessions()
    return {
        "count": len(sessions),
        "sessions": [
            {
                "session_id": s.session_id,
                "service_detected": s.service_detected,
                "rows_count": s.rows_count,
                "period_start": s.period_start,
                "period_end": s.period_end,
                "source_file": s.source_file,
                "created_at": s.created_at.isoformat(),
            }
            for s in sessions
        ]
    }


@router.get("/sessions/{session_id}")
async def get_session_details(session_id: str, db: Session = Depends(get_db)):
    """Get a single upload session with preview and derived family list."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found")

    # Column candidates in priority order — covers both legacy and 5G CSV formats
    _FAMILY_CANDIDATES = ("COD_FAM", "PRODUCT_FAMILY", "product_family", "family")

    families: list[str] = []
    source_path = Path(settings.DATA_LANDING_DIR) / session.source_file
    if source_path.exists():
        try:
            source_df = pd.read_csv(source_path)
            # Normalize column names to handle any casing/spacing variants
            source_df.columns = [c.strip() for c in source_df.columns]
            family_column = next(
                (c for c in _FAMILY_CANDIDATES if c in source_df.columns), None
            )
            if family_column:
                families = sorted(
                    {
                        str(value).strip()
                        for value in source_df[family_column].dropna().tolist()
                        if str(value).strip()
                    }
                )
        except Exception:
            families = []

    # Fallback 1: check session preview (all candidate keys)
    if not families:
        preview_families: set[str] = set()
        for row in session.preview:
            if not isinstance(row, dict):
                continue
            for key in _FAMILY_CANDIDATES:
                val = str(row.get(key) or "").strip()
                if val and val.upper() != "UNKNOWN":
                    preview_families.add(val)
        families = sorted(preview_families)

    # Fallback 2: query only families that have current fact_stock rows
    if not families:
        try:
            from sqlalchemy import text
            rows = db.execute(
                text(
                    "SELECT DISTINCT p.product_family "
                    "FROM mart.dim_products p "
                    "INNER JOIN mart.fact_stock fs ON fs.product_id = p.product_id "
                    "WHERE p.product_family IS NOT NULL "
                    "  AND p.product_family NOT IN ('', 'UNKNOWN') "
                    "ORDER BY p.product_family"
                )
            ).fetchall()
            families = [r[0] for r in rows if r[0] and str(r[0]).strip()]
        except Exception:
            families = []

    return {
        "session_id": session.session_id,
        "service_detected": session.service_detected,
        "rows_count": session.rows_count,
        "period_start": session.period_start,
        "period_end": session.period_end,
        "source_file": session.source_file,
        "created_at": session.created_at.isoformat(),
        "preview": session.preview,
        "families": families,
    }


@router.get("/target-values")
async def get_target_values(
    granularity: Literal["daily", "monthly"] = "daily",
    target_level: Literal["service", "product", "category", "region"] = "service",
    session_id: Optional[str] = None,
    service_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Return valid target values for the selected granularity and target level.

    Scopes product/category/region lists to a service resolved from the data
    (explicit service_type wins; an 'UNKNOWN' session no longer empties the list).
    For target_level='service', returns the services present in the data.
    """
    session_service = None
    if session_id:
        session = session_manager.get_session(session_id)
        if session:
            session_service = session.service_detected
    try:
        service_code = resolve_service_code(db, service_type, session_service)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    values = list_target_values(
        db=db,
        granularity=granularity,
        target_level=target_level,
        service_code=service_code,
    )
    return {
        "granularity": granularity,
        "target_level": target_level,
        "resolved_service": service_code or "ALL",
        "count": len(values),
        "values": values,
    }


@router.post("")
@router.post("/")
def start_training(
    request: TrainingRequest,
    db: Session = Depends(get_db),
):
    """
    Start model training pipeline
    
    - **session_id**: Upload session ID
    - **horizon**: Forecast horizon in periods for the selected granularity
    - **models**: List of models to train
    - **enable_generative**: Include LLM-based forecasting
    - **granularity**: Daily or monthly series
    - **target_level**: Service, product, category, or region
    - **target_value**: Optional value for product/category/region targeting
    """
    try:
        session = session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found")

        selected = request.models or ["all"]
        expected_total = len(CLASSIC_MODELS) + len(GENERATIVE_MODELS) if "all" in [m.lower() for m in selected] else len(selected)
        training_id = session_manager.create_training_job(request.session_id, total_models=expected_total, db=db)
        session_manager.update_training_job(
            training_id,
            db=db,
            status=TrainingStatus.RUNNING,
            progress_pct=5,
            models_completed=0,
        )
        # Record the request params on the job so a later model="best" forecast only
        # reuses a job trained for the same granularity/target (see forecast.py).
        # Resolve which service to forecast from the data (Option C), not just the
        # session's single detected service. Explicit service_type wins; a stock/
        # multi-service session (service_detected='UNKNOWN') resolves to all services.
        service_code = resolve_service_code(db, request.service_type, session.service_detected)

        _job = session_manager.get_training_job(training_id)
        if _job is not None:
            _job.granularity = request.granularity
            _job.target_level = request.target_level
            _job.target_value = request.target_value
            _job.service_type = service_code

        resolved_target_value = resolve_target_value(
            db=db,
            granularity=request.granularity,
            target_level=request.target_level,
            target_value=request.target_value,
            service_code=service_code,
        )

        results = train_models(
            db=db,
            horizon=request.horizon,
            enable_generative=request.enable_generative,
            service_code=service_code,
            selected_models=selected,
            granularity=request.granularity,
            target_level=request.target_level,
            target_value=resolved_target_value,
            include_promotions=request.include_promotions,
            include_price=request.include_price,
            include_calendar=request.include_calendar,
            session_id=request.session_id,
        )

        best_model = results[0]["model"] if results else "naive_last"
        session_manager.update_training_job(
            training_id,
            db=db,
            status=TrainingStatus.COMPLETED,
            progress_pct=100,
            models_completed=len(results),
            total_models=len(results),
            results=results,
            best_model=best_model,
        )

        return {
            "status": "completed",
            "training_id": training_id,
            "best_model": best_model,
            "resolved_target_value": resolved_target_value,
            "results": results,
        }
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Training validation failed: {e}")
        if "training_id" in locals():
            session_manager.update_training_job(
                training_id,
                db=db,
                status=TrainingStatus.FAILED,
                error_message=str(e),
            )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Training failed: {e}")
        if "training_id" in locals():
            session_manager.update_training_job(
                training_id,
                db=db,
                status=TrainingStatus.FAILED,
                error_message=str(e),
            )
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{training_id}")
async def get_training_status(training_id: str):
    """
    Get training job status and progress
    """
    job = session_manager.get_training_job(training_id)
    if not job:
        raise HTTPException(status_code=404, detail="Training job not found")

    return {
        "training_id": job.training_id,
        "session_id": job.session_id,
        "status": job.status,
        "progress": job.progress_pct,
        "models_completed": job.models_completed,
        "total_models": job.total_models,
        "results": job.results,
        "best_model": job.best_model,
        "error_message": job.error_message,
    }
