"""Anomaly Detection specialist agent (Phase 2).

Exposes two synchronous, LLM-friendly tools over the existing anomaly service:
  - ``detect_anomalies`` → scored/classified spikes, drops & data-quality issues
    (wraps ``anomaly_service.detect_anomalies`` + ``get_summary_stats``, the same
    functions behind GET /api/anomaly/detect).
  - ``get_sales_timeseries`` → the sales series with per-point anomaly flags for
    trend context (wraps ``anomaly_service.get_timeseries``).

Both take simple scalar arguments so an 8B model can call them reliably.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from app.agents.base import Tool, AgentResult, run_agent
from app.services.anomaly_service import (
    detect_anomalies,
    get_summary_stats,
    get_timeseries,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "anomaly"

ANOMALY_SYSTEM_PROMPT = (
    "You are the Anomaly Detection specialist for a Tunisian telecom operator "
    "(services: Fibre, 5G, Data Bundle, VOD). You find and explain unusual patterns "
    "in sales — unexpected spikes, drops, gradual shifts, and data-quality issues.\n\n"
    "Rules:\n"
    "- ALWAYS use the tools to get real figures. Never invent anomalies, dates, or numbers.\n"
    "- Call `detect_anomalies` to find and score anomalies. Call `get_sales_timeseries` "
    "when you need the surrounding trend to interpret a spike or drop.\n"
    "- Filter by `service_code` (e.g. FIBRE, 5G, DATA_BUNDLE, VOD) when the question names a service.\n"
    "- After the tools return, answer in the user's language, concisely. Lead with HIGH-severity "
    "anomalies, and for each give the date, expected vs actual, and the likely cause / recommended action.\n"
    "- If the question is not about anomalies or unusual sales patterns, say it is outside your scope."
)

_MAX_ANOMALIES = 15   # cap tool output so the LLM context stays manageable
_MAX_POINTS = 24      # most recent points returned by the timeseries tool

# Short filter labels the LLM can pass -> the full labels the service expects.
_TYPE_MAP = {
    "spike": "Unexpected Spike",
    "drop": "Unexpected Drop",
    "data_quality": "Data Quality Issue",
    "gradual": "Gradual Anomaly",
}

# Fields surfaced to the model (drop internal ids / rag fields to keep it compact).
_ANOMALY_FIELDS = (
    "service_code", "region_label", "detected_date", "anomaly_type", "severity",
    "expected", "actual", "variance_pct", "z_score", "possible_cause", "action_recommended",
)


def _tool_detect_anomalies(
    context: dict[str, Any],
    service_code: str | None = None,
    granularity: str = "monthly",
    severity: str | None = None,
    anomaly_type: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Detect and score anomalies in the sales data, with a summary."""
    db = context["db"]
    granularity = granularity if granularity in ("monthly", "daily") else "monthly"
    limit = max(1, min(50, int(limit)))
    type_filter = _TYPE_MAP.get(anomaly_type or "", anomaly_type) if anomaly_type else None

    records = detect_anomalies(
        db=db,
        service_code=service_code,
        severity_filter=severity,
        anomaly_type_filter=type_filter,
        granularity=granularity,
        limit=limit,
    )
    summary = get_summary_stats(records)
    anomalies = [
        {k: v for k, v in asdict(r).items() if k in _ANOMALY_FIELDS}
        for r in records[:_MAX_ANOMALIES]
    ]
    return {
        "service_code": service_code,
        "granularity": granularity,
        "summary": summary,
        "anomalies": anomalies,
        "truncated": len(records) > _MAX_ANOMALIES,
    }


def _tool_get_sales_timeseries(
    context: dict[str, Any],
    service_code: str | None = None,
    granularity: str = "monthly",
) -> dict[str, Any]:
    """Return the sales time series with per-point anomaly flags (most recent points)."""
    db = context["db"]
    granularity = granularity if granularity in ("monthly", "daily") else "monthly"
    series = get_timeseries(db, service_code=service_code, granularity=granularity)
    return {
        "service_code": service_code,
        "granularity": granularity,
        "point_count": len(series),
        "series": series[-_MAX_POINTS:],
        "truncated": len(series) > _MAX_POINTS,
    }


ANOMALY_TOOLS: list[Tool] = [
    Tool(
        name="detect_anomalies",
        description=(
            "Find and score anomalies (spikes, drops, gradual shifts, data-quality issues) in the "
            "sales data, with a severity summary. Use whenever the user asks what is unusual, "
            "abnormal, spiking, dropping, or wrong in sales."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_code": {
                    "type": "string",
                    "description": "Filter to one service, e.g. FIBRE, 5G, DATA_BUNDLE, VOD. Omit for all.",
                },
                "granularity": {"type": "string", "enum": ["monthly", "daily"]},
                "severity": {"type": "string", "enum": ["high", "medium"]},
                "anomaly_type": {
                    "type": "string",
                    "enum": ["spike", "drop", "data_quality", "gradual"],
                },
                "limit": {"type": "integer", "description": "Max anomalies to return (1-50)."},
            },
            "required": [],
        },
        fn=_tool_detect_anomalies,
    ),
    Tool(
        name="get_sales_timeseries",
        description=(
            "Get the sales time series with per-point anomaly flags. Use to see the trend around "
            "a spike or drop, or to judge whether a value is truly unusual."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_code": {
                    "type": "string",
                    "description": "Filter to one service, e.g. FIBRE. Omit for all.",
                },
                "granularity": {"type": "string", "enum": ["monthly", "daily"]},
            },
            "required": [],
        },
        fn=_tool_get_sales_timeseries,
    ),
]


def run_anomaly_agent(question: str, db: Any, max_iterations: int | None = None) -> AgentResult:
    """Answer an anomaly question using the Anomaly agent's tool loop."""
    return run_agent(
        agent_name=AGENT_NAME,
        system_prompt=ANOMALY_SYSTEM_PROMPT,
        user_message=question,
        tools=ANOMALY_TOOLS,
        context={"db": db},
        max_iterations=max_iterations,
    )
