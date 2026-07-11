"""Agentic chat endpoint.

POST /api/agent/chat routes a natural-language question to a specialist agent
that reasons and calls tools over the existing services. Phase 1 wires the Stock
specialist directly; a supervisor that routes across specialists lands in a later
phase (see the design proposal).

Runs **async (202 + poll)**, mirroring the inventory forecast/RAG endpoints: the
agent loop makes several sequential Ollama calls (tens of seconds on CPU), which
exceeds proxy/browser idle timeouts. POST returns a job_id immediately; the
client polls GET /api/agent/chat/status/{job_id}. The background task runs in
FastAPI's threadpool (blocking Ollama calls stay off the event loop) and opens
its own DB session, matching _run_background_* in inventory.py.
"""
import logging
import threading
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.anomaly import AGENT_NAME as ANOMALY_AGENT, run_anomaly_agent
from app.agents.knowledge import AGENT_NAME as KNOWLEDGE_AGENT, run_knowledge_agent
from app.agents.sales import AGENT_NAME as SALES_AGENT, run_sales_agent
from app.agents.stock import AGENT_NAME as STOCK_AGENT, run_stock_agent
from app.agents.supervisor import AGENT_NAME as SUPERVISOR_AGENT, run_supervisor
from app.core.config import settings
from app.core.tracing import flush_tracing
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["Agent"])

# Registry of specialists reachable from the endpoint. Grows as later phases add
# agents; a supervisor will eventually sit in front of these.
_AGENTS = {
    SUPERVISOR_AGENT: run_supervisor,
    STOCK_AGENT: run_stock_agent,
    ANOMALY_AGENT: run_anomaly_agent,
    SALES_AGENT: run_sales_agent,
    KNOWLEDGE_AGENT: run_knowledge_agent,
}

# ── Async agent job store ─────────────────────────────────────────────────────
# Keyed by job_id; small entries (status + result dict or error string). In-memory
# only — safe under the single uvicorn worker (see the --workers note in Dockerfile).
_agent_jobs: dict[str, dict[str, Any]] = {}
_agent_jobs_lock = threading.Lock()


class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="The user's question.")
    agent: str = Field("supervisor", description="Which agent to run. Default 'supervisor' routes across all specialists.")
    max_iterations: int | None = Field(
        None, ge=1, le=8, description="Override the LLM<->tool round-trip cap for this call."
    )


def _run_agent_job(job_id: str, agent: str, message: str, max_iterations: int | None) -> None:
    """Background worker: run the specialist agent and store the result."""
    runner = _AGENTS[agent]
    db = SessionLocal()
    try:
        result = runner(message, db, max_iterations=max_iterations)
        final = {
            "agent": result.agent,
            "answer": result.answer,
            "steps": result.steps,
            "iterations": result.iterations,
            "tokens": result.tokens,
        }
        with _agent_jobs_lock:
            _agent_jobs[job_id] = {"status": "completed", "result": final, "error": None}
        logger.info("✓ Agent job done: job=%s agent=%s iterations=%s", job_id, agent, result.iterations)
    except Exception as exc:
        logger.error("Agent job failed: job=%s %s", job_id, exc, exc_info=True)
        with _agent_jobs_lock:
            _agent_jobs[job_id] = {"status": "failed", "result": None, "error": str(exc)}
    finally:
        db.close()
        # Drain agent/tool spans to Phoenix promptly (same pattern as the QA/inventory paths).
        flush_tracing()


@router.post("/chat", status_code=status.HTTP_202_ACCEPTED)
def agent_chat(request: AgentChatRequest, background_tasks: BackgroundTasks) -> dict:
    """Queue an agent run. Returns 202 + job_id; poll /chat/status/{job_id}."""
    if not settings.AGENT_ENABLED:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Agent layer is disabled.")
    if request.agent not in _AGENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown agent '{request.agent}'. Available: {sorted(_AGENTS)}.",
        )

    job_id = str(uuid.uuid4())
    with _agent_jobs_lock:
        _agent_jobs[job_id] = {"status": "running", "result": None, "error": None}
    background_tasks.add_task(_run_agent_job, job_id, request.agent, request.message, request.max_iterations)
    logger.info("Agent job queued: job=%s agent=%s", job_id, request.agent)
    return {
        "job_id": job_id,
        "agent": request.agent,
        "status": "running",
        "message": f"Agent started. Poll GET /api/agent/chat/status/{job_id} for the answer.",
    }


@router.get("/chat/status/{job_id}")
def agent_chat_status(job_id: str) -> dict:
    """Return the status (and result once completed) of an agent run."""
    with _agent_jobs_lock:
        job = _agent_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown job_id '{job_id}'.")
    return {"job_id": job_id, **job}
