"""
RAG-based explainability endpoints
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import logging

from app.core.state import session_manager
from app.services.rag_service import rag_service

router = APIRouter()
logger = logging.getLogger(__name__)


class ExplainRequest(BaseModel):
    """Explain forecast request"""
    session_id: str
    service_type: str


@router.post("")
@router.post("/")
async def explain_forecast(request: ExplainRequest):
    """
    Generate AI explanation for forecast using RAG
    
    - **session_id**: Upload session ID
    - **service_type**: Service type (FIBRE, 5G, DATA_BUNDLE, VOD)
    """
    try:
        session = session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found")

        forecast_payload = session_manager.get_cached_forecast(f"{request.session_id}:forecast:last") or {}
        explanation = rag_service.explain_forecast(
            service_type=request.service_type,
            forecast_payload=forecast_payload,
        )
        return explanation
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Explain failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
