"""Run orchestration: the one function every entry point calls to ask the agent a question.

The CLI, the evaluation suite and any UI all go through :func:`ask`, so every
path gets the same configuration, prompt and tools.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from salesops_agent.agent import build_agent
from salesops_agent.config import Settings, load_settings, make_chat_model
from salesops_agent.costs import collect_usage, estimate_cost_usd
from salesops_agent.prompts import load_prompt
from salesops_agent.tools import build_context, build_tools, load_registry
from salesops_agent.versioning import component_versions


@dataclass
class RunResult:
    """What a caller gets back from one question."""

    run_id: str
    answer: str
    status: str
    tools_called: list[str] = field(default_factory=list)
    tools_executed: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    usage_is_estimate: bool = False
    failure_reason: str | None = None
    versions: dict[str, Any] = field(default_factory=dict)


def _final_text(messages: list[Any]) -> str:
    """Text of the last AI message that is an answer rather than a tool request."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def ask(question: str, settings: Settings | None = None, model: Any = None) -> RunResult:
    """Answer one SalesOps question.

    Args:
        question: the user's request.
        settings: loaded configuration (defaults to the project's agent.yaml).
        model: optional chat model override; the configured provider is used when omitted.
    """
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:12]
    settings = settings or load_settings()
    prompt = load_prompt(settings)
    specs = load_registry(settings.path("tool_registry"))
    tools = build_tools(specs, build_context(settings))
    agent = build_agent(model or make_chat_model(settings), tools, prompt.text)

    state = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        {"recursion_limit": settings.recursion_limit},
    )
    messages = state["messages"]
    usage = collect_usage(messages)
    return RunResult(
        run_id=run_id,
        answer=_final_text(messages),
        status="completed",
        tools_called=[tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls],
        tools_executed=[m.name for m in messages if isinstance(m, ToolMessage)],
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=estimate_cost_usd(usage, settings.model.name, settings.pricing),
        usage_is_estimate=usage.estimated,
        versions=component_versions(settings, prompt),
    )
