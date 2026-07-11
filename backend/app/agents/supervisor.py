"""Supervisor orchestrator agent (Phase 3).

The top-level agent. Its tools ARE the specialists — ``ask_stock_agent``,
``ask_anomaly_agent``, ``ask_sales_agent``, ``ask_knowledge_agent`` — each of
which runs that specialist's own tool loop and returns its answer. The supervisor
parses intent, delegates the relevant sub-question to one or more specialists,
then composes a single grounded answer.

Because each specialist call re-enters ``run_agent``, Phoenix shows a fully
nested tree: agent.supervisor → tool.ask_stock_agent → agent.stock → tool.* …
— the flagship observability story. This is inherently multi-hop and slow
(supervisor LLM + each specialist's LLM+tool calls), so the endpoint's async
202+poll path is what makes it usable.
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.base import Tool, AgentResult, run_agent
from app.agents.anomaly import run_anomaly_agent
from app.agents.knowledge import run_knowledge_agent
from app.agents.sales import run_sales_agent
from app.agents.stock import run_stock_agent

logger = logging.getLogger(__name__)

AGENT_NAME = "supervisor"

SUPERVISOR_SYSTEM_PROMPT = (
    "You are the orchestrator for a Tunisian telecom operator's analytics assistant "
    "(services: Fibre, 5G, Data Bundle, VOD). You do NOT answer domain questions yourself — "
    "you delegate to four specialist agents and then synthesize their findings.\n\n"
    "Specialists (each is a tool that takes a natural-language `question`):\n"
    "- `ask_stock_agent`: inventory — demand, stock levels, restocking, rupture/stockout risk.\n"
    "- `ask_anomaly_agent`: unusual sales patterns — spikes, drops, data-quality issues.\n"
    "- `ask_sales_agent`: sales forecasting — future sales, demand trends, best-model forecasts.\n"
    "- `ask_knowledge_agent`: documentation, procedures, policies, and live sales/stock KPIs.\n\n"
    "How to work:\n"
    "- Decide which specialist(s) the question needs. Simple questions go to ONE specialist; "
    "complex ones (e.g. 'why did FIBRE stock spike and what should I reorder?') need SEVERAL — "
    "delegate the relevant sub-question to each, rephrased for that specialist.\n"
    "- Always delegate domain questions — never invent stock, sales, anomaly, or policy figures.\n"
    "- After the specialists respond, compose ONE concise answer in the user's language that "
    "integrates their findings and notes which specialist provided what.\n"
    "- If the question is outside all four domains, say so instead of delegating."
)


def _delegate(runner, context: dict[str, Any], question: str) -> dict[str, Any]:
    """Run a specialist agent and return its answer + which tools it used."""
    result: AgentResult = runner(question, context["db"])
    return {
        "agent": result.agent,
        "answer": result.answer,
        "tools_used": [s["tool"] for s in result.steps],
    }


def _tool_ask_stock_agent(context: dict[str, Any], question: str) -> dict[str, Any]:
    """Delegate an inventory/stock question to the Stock specialist."""
    return _delegate(run_stock_agent, context, question)


def _tool_ask_anomaly_agent(context: dict[str, Any], question: str) -> dict[str, Any]:
    """Delegate an anomaly/unusual-pattern question to the Anomaly specialist."""
    return _delegate(run_anomaly_agent, context, question)


def _tool_ask_sales_agent(context: dict[str, Any], question: str) -> dict[str, Any]:
    """Delegate a sales-forecast question to the Sales specialist."""
    return _delegate(run_sales_agent, context, question)


def _tool_ask_knowledge_agent(context: dict[str, Any], question: str) -> dict[str, Any]:
    """Delegate a documentation/KPI question to the Knowledge specialist."""
    return _delegate(run_knowledge_agent, context, question)


def _specialist_tool(name: str, domain: str, fn) -> Tool:
    return Tool(
        name=name,
        description=(
            f"Ask the {domain} specialist. Pass the sub-question (rephrased for that specialist) "
            f"as `question`; it runs its own tools and returns a grounded answer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": f"The {domain} sub-question to answer."},
            },
            "required": ["question"],
        },
        fn=fn,
    )


SUPERVISOR_TOOLS: list[Tool] = [
    _specialist_tool("ask_stock_agent", "stock & inventory", _tool_ask_stock_agent),
    _specialist_tool("ask_anomaly_agent", "anomaly detection", _tool_ask_anomaly_agent),
    _specialist_tool("ask_sales_agent", "sales forecasting", _tool_ask_sales_agent),
    _specialist_tool("ask_knowledge_agent", "knowledge base", _tool_ask_knowledge_agent),
]

# Higher default cap than a specialist: the supervisor may consult several
# specialists (one per round) before composing the final answer.
_SUPERVISOR_MAX_ITERATIONS = 5


def run_supervisor(question: str, db: Any, max_iterations: int | None = None) -> AgentResult:
    """Answer a question by orchestrating the specialist agents."""
    return run_agent(
        agent_name=AGENT_NAME,
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        user_message=question,
        tools=SUPERVISOR_TOOLS,
        context={"db": db},
        max_iterations=max_iterations or _SUPERVISOR_MAX_ITERATIONS,
    )
