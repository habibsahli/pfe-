"""Stock / inventory specialist agent (Phase 1 vertical slice).

Exposes two tools over the existing stock services:
  - ``get_demand_statistics`` → rolling avg demand + latest stock per segment
    (wraps ``stock_recommendation_service.fetch_demand_segments``).
  - ``recommend_restock`` → runs the real StockRecommendationEngine over those
    segments and returns reorder actions / risk (wraps the engine used by
    ``POST /api/v1/inventory/recommendations``).

Both take simple scalar arguments so an 8B model can call them reliably; the
tool builds the heavy structured engine input itself.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.agents.base import Tool, AgentResult, run_agent
from app.services.stock_recommendation_service import (
    ForecastPoint,
    RecommendationInput,
    StockRecommendationEngine,
    Z_SCORE_BY_SERVICE_LEVEL,
    fetch_demand_segments,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "stock"

STOCK_SYSTEM_PROMPT = (
    "You are the Stock & Inventory specialist for a Tunisian telecom operator "
    "(services: Fibre, 5G, Data Bundle, VOD). You help planners understand demand "
    "and decide what to restock.\n\n"
    "Rules:\n"
    "- ALWAYS use the tools to get real figures. Never invent stock levels, demand, "
    "or reorder quantities.\n"
    "- Call `get_demand_statistics` to see current stock and average demand per segment. "
    "Call `recommend_restock` to get reorder points, quantities to order, and rupture risk.\n"
    "- Pick the `forecast_scope` that matches the question: 'national' for a whole-family view, "
    "'by_product_type' to break down by product type, 'by_governorate' for a regional view.\n"
    "- After the tools return, answer in the user's language, concisely, citing the actual "
    "numbers. Highlight products at CRITICAL or HIGH rupture risk first.\n"
    "- If the question is not about stock, inventory, demand, or restocking, say it is outside "
    "your scope instead of guessing."
)

_MAX_SEGMENTS = 25  # cap tool output so the LLM context stays manageable


def _tool_get_demand_statistics(
    context: dict[str, Any],
    forecast_scope: str = "national",
    months: int = 3,
) -> dict[str, Any]:
    """Return rolling avg demand + latest stock per product segment."""
    db = context["db"]
    months = max(1, min(12, int(months)))
    segments = fetch_demand_segments(db, forecast_scope=forecast_scope, months=months)
    truncated = len(segments) > _MAX_SEGMENTS
    return {
        "forecast_scope": forecast_scope,
        "months_window": months,
        "segment_count": len(segments),
        "segments": segments[:_MAX_SEGMENTS],
        "truncated": truncated,
    }


def _resolve_z(service_level: float) -> float:
    """Nearest configured z-score for a service level (defaults to 95% = 1.65)."""
    for sl, z in Z_SCORE_BY_SERVICE_LEVEL.items():
        if abs(service_level - sl) < 0.001:
            return z
    return 1.65


def _flat_forecast_series(avg_monthly_demand: float, horizon_months: int) -> list[ForecastPoint]:
    """Build a flat monthly forecast from the average demand (agent-level heuristic)."""
    today = date.today()
    points: list[ForecastPoint] = []
    for i in range(horizon_months):
        m = today.month - 1 + i
        y = today.year + m // 12
        points.append(ForecastPoint(date=f"{y:04d}-{(m % 12) + 1:02d}-01", value=round(avg_monthly_demand, 2)))
    return points


def _tool_recommend_restock(
    context: dict[str, Any],
    forecast_scope: str = "by_product_type",
    months: int = 3,
    service_level: float = 0.95,
    horizon_months: int = 6,
) -> dict[str, Any]:
    """Compute reorder recommendations from current demand & stock per segment."""
    db = context["db"]
    months = max(1, min(12, int(months)))
    horizon_months = max(1, min(12, int(horizon_months)))
    segments = fetch_demand_segments(db, forecast_scope=forecast_scope, months=months)
    if not segments:
        return {"forecast_scope": forecast_scope, "recommendations": [], "summary": {},
                "note": "No demand data available for this scope."}

    inputs = [
        RecommendationInput(
            product_id=f"{s['family']}|{s['product_type']}|{s['governorate']}",
            product_name=s["family"],
            product_type=s["product_type"],
            governorate=s["governorate"],
            current_stock=s["current_stock"],
            forecast_series=_flat_forecast_series(s["avg_monthly_demand"], horizon_months),
            avg_monthly_demand=s["avg_monthly_demand"],
        )
        for s in segments
    ]

    response = StockRecommendationEngine.generate_recommendations(
        inputs=inputs,
        z_score=_resolve_z(float(service_level)),
    )

    # Summarise each recommendation to the fields a planner acts on (keeps the
    # tool result compact for the LLM).
    recs = [
        {
            "product": r.product_name,
            "product_type": r.product_type,
            "governorate": r.governorate,
            "current_stock": r.current_stock,
            "avg_monthly_demand": r.avg_monthly_demand,
            "reorder_point": r.reorder_point,
            "qty_to_order": r.qty_to_order,
            "order_urgency": r.order_urgency,
            "rupture_risk": r.rupture_risk,
            "coverage_months": r.coverage_months,
        }
        for r in response.recommendations[:_MAX_SEGMENTS]
    ]
    return {
        "forecast_scope": forecast_scope,
        "service_level": service_level,
        "horizon_months": horizon_months,
        "summary": response.summary,
        "recommendations": recs,
        "truncated": len(response.recommendations) > _MAX_SEGMENTS,
    }


STOCK_TOOLS: list[Tool] = [
    Tool(
        name="get_demand_statistics",
        description=(
            "Get current stock level and rolling average monthly demand per product segment. "
            "Use before answering any question about demand or stock levels."
        ),
        parameters={
            "type": "object",
            "properties": {
                "forecast_scope": {
                    "type": "string",
                    "enum": ["national", "by_product_type", "by_governorate"],
                    "description": "Aggregation level for the segments.",
                },
                "months": {
                    "type": "integer",
                    "description": "Rolling window (1-12 months) for the average demand.",
                },
            },
            "required": [],
        },
        fn=_tool_get_demand_statistics,
    ),
    Tool(
        name="recommend_restock",
        description=(
            "Compute restock recommendations (reorder point, quantity to order, urgency, "
            "rupture risk) for each product segment, using current stock and demand. "
            "Use when the user asks what to reorder or which products are at risk."
        ),
        parameters={
            "type": "object",
            "properties": {
                "forecast_scope": {
                    "type": "string",
                    "enum": ["national", "by_product_type", "by_governorate"],
                    "description": "Aggregation level for the recommendations.",
                },
                "months": {
                    "type": "integer",
                    "description": "Rolling window (1-12 months) used to estimate demand.",
                },
                "service_level": {
                    "type": "number",
                    "description": "Target service level (0.85, 0.90, 0.95, 0.98, 0.99). Default 0.95.",
                },
                "horizon_months": {
                    "type": "integer",
                    "description": "Forecast horizon in months (1-12). Default 6.",
                },
            },
            "required": [],
        },
        fn=_tool_recommend_restock,
    ),
]


def run_stock_agent(question: str, db: Any, max_iterations: int | None = None) -> AgentResult:
    """Answer a stock/inventory question using the Stock agent's tool loop."""
    return run_agent(
        agent_name=AGENT_NAME,
        system_prompt=STOCK_SYSTEM_PROMPT,
        user_message=question,
        tools=STOCK_TOOLS,
        context={"db": db},
        max_iterations=max_iterations,
    )
