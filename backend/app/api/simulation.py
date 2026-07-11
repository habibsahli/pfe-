"""
What-If simulation and scenario analysis endpoints
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import logging
from sqlalchemy.orm import Session

from app.core.state import session_manager
from app.db.session import get_db
from app.services.forecasting_service import generate_forecast, what_if_impact

router = APIRouter()
logger = logging.getLogger(__name__)


class ScenarioRequest(BaseModel):
    """Scenario simulation request"""
    session_id: str
    scenario_text: str
    scenario_structured: Optional[dict] = None


@router.post("")
@router.post("/")
async def simulate_scenario(
    request: ScenarioRequest,
    db: Session = Depends(get_db),
):
    """
    Simulate forecast impact of a scenario
    
    - **session_id**: Upload session ID
    - **scenario_text**: Natural language scenario description
    - **scenario_structured**: Optional structured scenario data
    """
    try:
        session = session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found")

        baseline_payload = session_manager.get_cached_forecast(f"{request.session_id}:forecast:last")
        if not baseline_payload:
            baseline_payload = generate_forecast(
                db=db,
                best_model_name="ensemble",
                horizon=30,
                service_code=session.service_detected,
            )

        baseline = baseline_payload.get("forecast", [])
        scenario, impact_pct = what_if_impact(baseline, request.scenario_text)
        trend_word = "hausse" if impact_pct >= 0 else "baisse"

        return {
            "forecast_baseline": baseline,
            "forecast_scenario": scenario,
            "impact_analysis": f"Scenario estime une {trend_word} de {impact_pct}% sur le volume projete.",
            "impact_pct": impact_pct,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Simulation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
