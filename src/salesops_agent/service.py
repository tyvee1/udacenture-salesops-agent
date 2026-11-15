"""Run orchestration: the one function every entry point calls to ask the agent a question.

The CLI, the evaluation suite and any UI all go through :func:`ask`, so every
path gets the same configuration, prompt, tools and runtime controls:

1. **Input guardrail** — screens the request. A blocked request never reaches the
   agent; no model is even constructed, so no tokens are spent and no key is needed.
2. **Agent loop** — the model proposes tool calls; the tool guardrail middleware
   checks each one before it runs.
3. **Output guardrail** — screens the final answer before it is returned.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage

from salesops_agent.agent import build_agent
from salesops_agent.config import Settings, load_settings, make_chat_model
from salesops_agent.costs import collect_usage, estimate_cost_usd
from salesops_agent.guardrails import (
    SensitiveIndex,
    ToolCheckContext,
    ToolGuardrailMiddleware,
    check_input,
    check_output,
    load_policy,
)
from salesops_agent.prompts import load_prompt
from salesops_agent.recorder import RunRecorder
from salesops_agent.tools import build_context, build_tools, load_registry
from salesops_agent.versioning import component_versions


@dataclass
class RunResult:
    """What a caller gets back from one question."""

    run_id: str
    answer: str
    status: str  # completed | blocked | error
    tools_called: list[str] = field(default_factory=list)
    tools_executed: list[str] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    blocked: dict[str, Any] | None = None
    hitl_decision: str | None = None
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
    """Answer one SalesOps question under the runtime controls.

    Args:
        question: the user's request.
        settings: loaded configuration (defaults to the project's agent.yaml).
        model: optional chat model override; the configured provider is used when omitted.
    """
    settings = settings or load_settings()
    rec = RunRecorder(run_id=uuid.uuid4().hex[:12])
    prompt = load_prompt(settings)
    policy = load_policy(settings.root / "config" / "policy.yaml")
    specs = load_registry(settings.path("tool_registry"))
    ctx = build_context(settings)
    index = SensitiveIndex.build(ctx.restricted)
    versions = component_versions(settings, prompt)

    proposed: list[str] = []  # every tool call the model proposed, including ones never dispatched

    def result(**kw: Any) -> RunResult:
        executed = [t["tool"] for t in rec.tool_events if t["status"] == "executed"]
        return RunResult(
            run_id=rec.run_id, tools_executed=executed, tools_called=list(proposed),
            guardrail_events=rec.guardrail_events,
            latency_ms=round((time.perf_counter() - rec.started) * 1000, 1), versions=versions, **kw,
        )

    # 1. Input guardrail — before the agent or model exists.
    decision = check_input(question, policy)
    rec.guardrail(decision)
    if decision.blocked:
        return result(answer=policy.input_refusal, status="blocked", blocked=decision.blocked_payload())

    # 2. Agent loop with the tool guardrail in front of every tool.
    check_ctx = ToolCheckContext(policy=policy, index=index, specs=specs, known_recipients=ctx.crm.known_emails())
    agent = build_agent(
        model or make_chat_model(settings),
        build_tools(specs, ctx),
        prompt.text,
        middleware=[ToolGuardrailMiddleware(check_ctx, rec)],
    )
    try:
        state = agent.invoke(
            {"messages": [{"role": "user", "content": question}]},
            {"recursion_limit": settings.recursion_limit},
        )
    except Exception as exc:  # model/provider/recursion failures become a recorded error, not a crash
        rec.error(f"{type(exc).__name__}: {exc}")
        return result(answer="The agent failed to complete this request.", status="error",
                      failure_reason=f"{type(exc).__name__}: {exc}")

    messages = state["messages"]
    proposed.extend(tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
    usage = collect_usage(messages)
    cost = dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                estimated_cost_usd=estimate_cost_usd(usage, settings.model.name, settings.pricing),
                usage_is_estimate=usage.estimated)

    # 3. Output guardrail — on the final answer.
    answer = _final_text(messages)
    out = check_output(answer, policy, index)
    rec.guardrail(out)
    if out.blocked:
        return result(answer=policy.output_refusal, status="blocked", blocked=out.blocked_payload(), **cost)
    return result(answer=answer, status="completed", **cost)
