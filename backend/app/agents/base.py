"""Minimal agent runtime: a tool-calling loop over ``ollama_client.chat``.

No external agent framework — this is a small, explicit ReAct-style loop that:
  1. sends the conversation + tool specs to the LLM,
  2. dispatches any tool calls the model returns to plain Python callables,
  3. feeds the results back, and repeats until the model answers in text
     (or a max-iterations guard trips, after which we force a final answer).

Every agent run is wrapped in an OpenInference ``AGENT`` span and every tool
call in a ``TOOL`` span, so the whole loop is visible in Phoenix alongside the
existing RAG/forecast traces. Callers should ``flush_tracing()`` after the run
(the endpoint does this in a ``finally``) so spans export promptly.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

from app.core.config import settings
from app.core.tracing import get_tracer
from app.services.ollama_client import ollama_client

logger = logging.getLogger(__name__)
tracer = get_tracer("fibre-forecast-agents")

_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_AGENT = OpenInferenceSpanKindValues.AGENT.value
_TOOL = OpenInferenceSpanKindValues.TOOL.value
_LLM = OpenInferenceSpanKindValues.LLM.value


@dataclass
class Tool:
    """A callable the agent can invoke.

    ``fn`` receives the runtime ``context`` dict as its first argument followed
    by the (validated) keyword arguments the LLM supplied. ``parameters`` is a
    JSON-Schema object describing those arguments, sent to the model as-is.
    """
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]

    def spec(self) -> dict[str, Any]:
        """Render the OpenAI/Ollama-style function spec for the tools payload."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class AgentResult:
    """Outcome of an agent run: the final answer plus a transparent step trace."""
    answer: str
    agent: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    tokens: dict[str, int] = field(default_factory=dict)


def _extract_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise Ollama's tool_calls into ``[{"name", "arguments"}]``.

    Ollama returns ``arguments`` as a dict, but older/edge builds may hand back a
    JSON string — handle both so the dispatch loop is robust.
    """
    raw_calls = message.get("tool_calls") or []
    calls: list[dict[str, Any]] = []
    for rc in raw_calls:
        fn = (rc or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name, "arguments": args})
    return calls


def run_agent(
    *,
    agent_name: str,
    system_prompt: str,
    user_message: str,
    tools: list[Tool],
    context: dict[str, Any] | None = None,
    max_iterations: int | None = None,
    model: str | None = None,
) -> AgentResult:
    """Run one specialist agent to completion and return its answer + step trace."""
    context = context or {}
    max_iterations = max_iterations or settings.AGENT_MAX_ITERATIONS
    tool_by_name = {t.name: t for t in tools}
    tool_specs = [t.spec() for t in tools]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    steps: list[dict[str, Any]] = []
    prompt_total = 0
    completion_total = 0

    with tracer.start_as_current_span(f"agent.{agent_name}") as agent_span:
        agent_span.set_attribute(_KIND, _AGENT)
        agent_span.set_attribute("agent.name", agent_name)
        agent_span.set_attribute("agent.max_iterations", max_iterations)
        agent_span.set_attribute("agent.tool_names", json.dumps(list(tool_by_name)))
        agent_span.set_attribute(SpanAttributes.INPUT_VALUE, user_message)

        answer = ""
        iterations = 0
        for iteration in range(1, max_iterations + 1):
            iterations = iteration
            # The final iteration drops the tools so the model is forced to
            # answer in text instead of looping on tool calls forever.
            offer_tools = tool_specs if iteration < max_iterations else None
            with tracer.start_as_current_span(f"agent.{agent_name}.llm") as llm_span:
                llm_span.set_attribute(_KIND, _LLM)
                llm_span.set_attribute(SpanAttributes.LLM_MODEL_NAME, model or ollama_client.llm_model)
                llm_span.set_attribute("agent.iteration", iteration)
                message, meta = ollama_client.chat(
                    messages=messages,
                    tools=offer_tools,
                    model=model,
                    max_tokens=900,
                )
                if meta.get("prompt_tokens"):
                    prompt_total += meta["prompt_tokens"]
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, meta["prompt_tokens"])
                if meta.get("completion_tokens"):
                    completion_total += meta["completion_tokens"]
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, meta["completion_tokens"])
                if meta.get("total_duration_ms"):
                    llm_span.set_attribute("llm.total_duration_ms", meta["total_duration_ms"])

            # Ollama assistant turns must be echoed back into the history so the
            # model sees its own tool calls before the tool results.
            messages.append(message)
            tool_calls = _extract_tool_calls(message)

            if not tool_calls:
                answer = (message.get("content") or "").strip()
                break

            for call in tool_calls:
                name = call["name"]
                args = call["arguments"]
                tool = tool_by_name.get(name)
                started = time.time()
                with tracer.start_as_current_span(f"tool.{name}") as tool_span:
                    tool_span.set_attribute(_KIND, _TOOL)
                    tool_span.set_attribute(SpanAttributes.TOOL_NAME, name)
                    tool_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(args, default=str))
                    if tool is None:
                        result: Any = {"error": f"Unknown tool '{name}'."}
                    else:
                        try:
                            result = tool.fn(context, **args)
                        except Exception as exc:  # surface the error to the model, don't crash the loop
                            logger.warning("Agent tool '%s' failed: %s", name, exc, exc_info=True)
                            result = {"error": f"Tool '{name}' failed: {exc}"}
                    result_str = json.dumps(result, default=str)
                    tool_span.set_attribute(SpanAttributes.OUTPUT_VALUE, result_str)
                elapsed_ms = round((time.time() - started) * 1000, 1)

                steps.append({
                    "iteration": iteration,
                    "tool": name,
                    "arguments": args,
                    "duration_ms": elapsed_ms,
                    "ok": not (isinstance(result, dict) and "error" in result),
                })
                messages.append({"role": "tool", "content": result_str})

        if not answer:
            # Guard tripped without a text answer — return the last content or a note.
            answer = (messages[-1].get("content") or "").strip() if messages else ""
            if not answer:
                answer = "I could not complete this request within the allotted steps."

        agent_span.set_attribute(SpanAttributes.OUTPUT_VALUE, answer)
        agent_span.set_attribute("agent.iterations", iterations)
        agent_span.set_attribute("agent.tool_calls", len(steps))
        if prompt_total:
            agent_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_total)
        if completion_total:
            agent_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_total)

    return AgentResult(
        answer=answer,
        agent=agent_name,
        steps=steps,
        iterations=iterations,
        tokens={
            "prompt": prompt_total,
            "completion": completion_total,
            "total": prompt_total + completion_total,
        },
    )
