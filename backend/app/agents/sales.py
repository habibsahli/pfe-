"""Sales Forecast specialist agent (Phase 2).

Exposes two synchronous tools over the forecasting service. The key enabler is
that ``generate_forecast`` computes on-the-fly from the sales views with an
explicit model — no upload session and no prior training job required (model
"best" is what needs a stored job; a named model like seasonal_naive/xgboost
trains inline on the loaded history):
  - ``list_services`` → the service codes that actually have sales data
    (wraps ``list_available_services``).
  - ``forecast_sales`` → a horizon forecast with trend
    (wraps ``forecasting_service.generate_forecast``).
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.base import Tool, AgentResult, run_agent
from app.services.forecasting_service import generate_forecast, list_available_services, train_models

logger = logging.getLogger(__name__)

AGENT_NAME = "sales"

# Models that train inline quickly & reliably on the monthly/daily views. Default
# to seasonal_naive (always works); xgboost/prophet are available for higher accuracy.
_ALLOWED_MODELS = {"seasonal_naive", "naive_last", "xgboost", "prophet", "exp_smoothing", "linear_regression", "ensemble"}
_DEFAULT_MODEL = "seasonal_naive"

SALES_SYSTEM_PROMPT = (
    "You are the Sales Forecasting specialist for a Tunisian telecom operator "
    "(services: Fibre, 5G, Data Bundle, VOD). You forecast future sales and explain the trend.\n\n"
    "Rules:\n"
    "- ALWAYS use the tools to produce figures. Never invent forecast values or trends.\n"
    "- If unsure which services exist, call `list_services` first.\n"
    "- Prefer `train_and_forecast_best`: it trains the whole model zoo (xgboost, prophet, sarima, "
    "lstm, etc.), picks the most accurate model by backtest error, and forecasts with it. Use it "
    "whenever the user wants a forecast, the best/most accurate model, or does not specify a model.\n"
    "- Use `forecast_sales` only for a quick estimate or when the user names a specific model.\n"
    "- Pass `service_type` when the question names a service, otherwise forecast all services combined.\n"
    "- Default to monthly granularity unless the user asks for daily. Monthly horizon max 12, daily max 6.\n"
    "- After the tool returns, answer in the user's language, concisely: name the winning model and "
    "its error (WAPE), state the trend (hausse/baisse) and % change, and quote a few forecast points "
    "with their dates.\n"
    "- If the question is not about sales forecasting or trends, say it is outside your scope."
)

_MAX_HISTORY_TAIL = 4  # recent actuals returned for context


def _tool_list_services(context: dict[str, Any]) -> dict[str, Any]:
    """List the service codes that have sales data available to forecast."""
    db = context["db"]
    services = list_available_services(db)
    return {"services": services}


def _tool_forecast_sales(
    context: dict[str, Any],
    service_type: str | None = None,
    horizon: int = 6,
    granularity: str = "monthly",
    model: str = _DEFAULT_MODEL,
) -> dict[str, Any]:
    """Forecast sales for a service (or all services) over a horizon."""
    db = context["db"]
    granularity = granularity if granularity in ("monthly", "daily") else "monthly"
    max_h = 12 if granularity == "monthly" else 6
    horizon = max(1, min(max_h, int(horizon)))
    model = model if model in _ALLOWED_MODELS else _DEFAULT_MODEL

    # Validate the service against the data so the LLM gets a clean, actionable error.
    service_code = None
    if service_type and service_type.upper() not in ("ALL", "*"):
        available = list_available_services(db)
        match = next((s for s in available if s.upper() == service_type.upper()), None)
        if match is None:
            return {"error": f"Unknown service '{service_type}'. Available: {available}."}
        service_code = match

    try:
        payload = generate_forecast(
            db=db,
            best_model_name=model,
            horizon=horizon,
            service_code=service_code,
            granularity=granularity,
            target_level="service",
        )
    except Exception as exc:
        return {"error": f"Forecast failed: {exc}"}

    meta = payload.get("metadata", {})
    history = payload.get("historical", []) or []
    return {
        "service": service_code or "ALL",
        "granularity": granularity,
        "horizon": horizon,
        "model_used": meta.get("model_used", model),
        "trend": meta.get("trend"),
        "change_pct": meta.get("change_pct"),
        "recent_history": history[-_MAX_HISTORY_TAIL:],
        "forecast": payload.get("forecast", []),
    }


def _tool_train_and_forecast_best(
    context: dict[str, Any],
    service_type: str | None = None,
    horizon: int = 6,
    granularity: str = "monthly",
) -> dict[str, Any]:
    """Train the model zoo, select the most accurate model, and forecast with it."""
    db = context["db"]
    granularity = granularity if granularity in ("monthly", "daily") else "monthly"
    max_h = 12 if granularity == "monthly" else 6
    horizon = max(1, min(max_h, int(horizon)))

    service_code = None
    if service_type and service_type.upper() not in ("ALL", "*"):
        available = list_available_services(db)
        match = next((s for s in available if s.upper() == service_type.upper()), None)
        if match is None:
            return {"error": f"Unknown service '{service_type}'. Available: {available}."}
        service_code = match

    try:
        # Train the classic model zoo (results come back sorted best-first by composite_score,
        # the same selection the /api/training endpoint uses). Generative models are left off to
        # keep the tool responsive and dependency-free.
        results = train_models(
            db=db,
            horizon=horizon,
            enable_generative=False,
            service_code=service_code,
            granularity=granularity,
            target_level="service",
        )
    except Exception as exc:
        return {"error": f"Training failed: {exc}"}

    if not results:
        return {"error": "Training produced no model results."}

    best_model = results[0]["model"]
    leaderboard = [
        {
            "model": r["model"],
            "wape": round(float(r.get("wape", 0)), 2),
            "mape": round(float(r.get("mape", 0)), 2),
            "composite_score": round(float(r.get("composite_score", 0)), 3),
        }
        for r in results[:5]
    ]

    try:
        payload = generate_forecast(
            db=db,
            best_model_name=best_model,
            horizon=horizon,
            service_code=service_code,
            granularity=granularity,
            target_level="service",
        )
    except Exception as exc:
        return {"error": f"Forecast with best model '{best_model}' failed: {exc}", "best_model": best_model}

    meta = payload.get("metadata", {})
    history = payload.get("historical", []) or []
    return {
        "service": service_code or "ALL",
        "granularity": granularity,
        "horizon": horizon,
        "best_model": best_model,
        "models_evaluated": len(results),
        "leaderboard": leaderboard,
        "trend": meta.get("trend"),
        "change_pct": meta.get("change_pct"),
        "recent_history": history[-_MAX_HISTORY_TAIL:],
        "forecast": payload.get("forecast", []),
    }


SALES_TOOLS: list[Tool] = [
    Tool(
        name="train_and_forecast_best",
        description=(
            "Train all available forecasting models (xgboost, prophet, sarima, lstm, exp_smoothing, "
            "etc.), pick the most accurate one by backtest error, and forecast with it. Returns the "
            "winning model, a leaderboard, the trend, and forecast points. Prefer this for the best "
            "/ most accurate forecast. Slower than forecast_sales because it trains the zoo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "Service to forecast, e.g. FIBRE, 5G, DATA_BUNDLE, VOD. Omit for all services.",
                },
                "horizon": {
                    "type": "integer",
                    "description": "Number of periods to forecast (monthly max 12, daily max 6).",
                },
                "granularity": {"type": "string", "enum": ["monthly", "daily"]},
            },
            "required": [],
        },
        fn=_tool_train_and_forecast_best,
    ),
    Tool(
        name="list_services",
        description="List the service codes that have sales data available (e.g. FIBRE, 5G, DATA_BUNDLE, VOD).",
        parameters={"type": "object", "properties": {}, "required": []},
        fn=_tool_list_services,
    ),
    Tool(
        name="forecast_sales",
        description=(
            "Forecast future sales for a service (or all services) over a horizon, returning the "
            "trend, % change, recent history, and forecast points. Use for any question about "
            "future sales, demand trends, or projections."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "Service to forecast, e.g. FIBRE, 5G, DATA_BUNDLE, VOD. Omit for all services.",
                },
                "horizon": {
                    "type": "integer",
                    "description": "Number of periods to forecast (monthly max 12, daily max 6).",
                },
                "granularity": {"type": "string", "enum": ["monthly", "daily"]},
                "model": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_MODELS),
                    "description": "Forecast model. Default seasonal_naive; xgboost/prophet for higher accuracy.",
                },
            },
            "required": [],
        },
        fn=_tool_forecast_sales,
    ),
]


def run_sales_agent(question: str, db: Any, max_iterations: int | None = None) -> AgentResult:
    """Answer a sales-forecasting question using the Sales agent's tool loop."""
    return run_agent(
        agent_name=AGENT_NAME,
        system_prompt=SALES_SYSTEM_PROMPT,
        user_message=question,
        tools=SALES_TOOLS,
        context={"db": db},
        max_iterations=max_iterations,
    )
