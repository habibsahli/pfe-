"""Knowledge / RAG specialist agent (Phase 2).

Answers questions from the indexed knowledge base (procedures, policies, docs) and
live KPIs. Tools are the fast, LLM-free building blocks of the RAG pipeline so the
agent composes the answer itself (avoids nesting a second LLM call inside a tool):
  - ``search_knowledge`` → hybrid retrieval chunks (wraps ``rag_service.retrieve``).
  - ``get_live_kpis`` → recent sales / current stock figures (wraps ``_fetch_live_kpis``).
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.base import Tool, AgentResult, run_agent
from app.services.rag_service import rag_service, _fetch_live_kpis

logger = logging.getLogger(__name__)

AGENT_NAME = "knowledge"

KNOWLEDGE_SYSTEM_PROMPT = (
    "You are the Knowledge Base assistant for a Tunisian telecom operator "
    "(services: Fibre, 5G, Data Bundle, VOD). You answer questions about procedures, "
    "policies, and business documentation, and can pull live sales/stock KPIs.\n\n"
    "Rules:\n"
    "- Call `search_knowledge` to retrieve relevant document chunks before answering. "
    "Answer ONLY from the retrieved context — do not use outside knowledge.\n"
    "- Call `get_live_kpis` when the user asks for current sales or stock figures.\n"
    "- ALWAYS cite the sources (the `source` field of the chunks you used) in your answer.\n"
    "- If retrieval returns nothing relevant, say the knowledge base has no information on it "
    "instead of guessing.\n"
    "- Answer in the user's language, concisely."
)

_MAX_CHUNKS = 6
_CHUNK_CHARS = 600


def _tool_search_knowledge(
    context: dict[str, Any],
    query: str,
    service_type: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Retrieve the most relevant knowledge-base chunks for a query."""
    top_k = max(1, min(_MAX_CHUNKS, int(top_k)))
    results = rag_service.retrieve(query, service_type=service_type, top_k=top_k)
    chunks = [
        {
            "source": r.get("source"),
            "score": round(float(r.get("score", 0)), 3),
            "text": (r.get("text") or "")[:_CHUNK_CHARS],
        }
        for r in results[:_MAX_CHUNKS]
    ]
    return {"query": query, "service_type": service_type, "chunk_count": len(chunks), "chunks": chunks}


def _tool_get_live_kpis(context: dict[str, Any], service_type: str | None = None) -> dict[str, Any]:
    """Return recent sales and current stock KPIs from the data mart."""
    kpis = _fetch_live_kpis(service_type)
    return {"service_type": service_type, "kpis": kpis or "No live KPI data available."}


KNOWLEDGE_TOOLS: list[Tool] = [
    Tool(
        name="search_knowledge",
        description=(
            "Search the knowledge base (procedures, policies, documentation) and return the most "
            "relevant text chunks with their sources. Use before answering any documentation or "
            "how-to question."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "service_type": {
                    "type": "string",
                    "description": "Optionally scope to a service, e.g. FIBRE, 5G, DATA_BUNDLE, VOD.",
                },
                "top_k": {"type": "integer", "description": "Number of chunks to return (1-6)."},
            },
            "required": ["query"],
        },
        fn=_tool_search_knowledge,
    ),
    Tool(
        name="get_live_kpis",
        description=(
            "Get recent sales (last 3 months) and current stock figures from the database. "
            "Use when the user asks for actual/current numbers rather than documentation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "Optionally scope to a service, e.g. FIBRE. Omit for all services.",
                },
            },
            "required": [],
        },
        fn=_tool_get_live_kpis,
    ),
]


def run_knowledge_agent(question: str, db: Any, max_iterations: int | None = None) -> AgentResult:
    """Answer a knowledge-base question using the Knowledge agent's tool loop."""
    return run_agent(
        agent_name=AGENT_NAME,
        system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
        user_message=question,
        tools=KNOWLEDGE_TOOLS,
        context={"db": db},
        max_iterations=max_iterations,
    )
