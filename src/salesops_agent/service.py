"""Run orchestration: the one function every entry point calls to ask the agent a question.

The CLI, the evaluation suite and any UI all go through :func:`ask`, so every
path gets the same configuration, prompt, tools and runtime controls:

1. **Input guardrail** — screens the request. A blocked request never reaches the
   agent; no model is even constructed, so no tokens are spent and no key is needed.
2. **Agent loop** — the model proposes tool calls; the tool guardrail middleware
   checks each one before it runs.
3. **Approval gate** — when the agent pauses before a high-risk action, the
   decision comes from ``approval`` (the ``--approval`` flag), else from an
   interactive ``approver`` callback, else it is ``reject``: a run with nobody to
   ask fails closed.
4. **Output guardrail** — screens the final answer before it is returned.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

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
    status: str  # completed | blocked | rejected | error
    tools_called: list[str] = field(default_factory=list)
    tools_executed: list[str] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    blocked: dict[str, Any] | None = None
    hitl_decision: str | None = None
    approvals: list[dict[str, Any]] = field(default_factory=list)
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


#: Called with the pending action when the agent pauses; returns "approve" or "reject".
Approver = Callable[[dict[str, Any]], str]


def _decide(pending: dict[str, Any], approval: str | None, approver: Approver | None) -> dict[str, str]:
    """Pick the decision for one paused action and say where it came from."""
    if approval is not None:
        return {"decision": approval, "source": "cli_flag"}
    if approver is not None:
        return {"decision": approver(pending), "source": "interactive"}
    return {"decision": "reject", "source": "default_no_signal"}


def ask(
    question: str,
    settings: Settings | None = None,
    model: Any = None,
    approval: str | None = None,
    approver: Approver | None = None,
) -> RunResult:
    """Answer one SalesOps question under the runtime controls.

    Args:
        question: the user's request.
        settings: loaded configuration (defaults to the project's agent.yaml).
        model: optional chat model override; the configured provider is used when omitted.
        approval: ``"approve"`` or ``"reject"`` applied to every paused high-risk action.
        approver: interactive callback used when ``approval`` is not given.
    """
    if approval is not None and approval not in ("approve", "reject"):
        raise ValueError("approval must be 'approve' or 'reject'")
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
            guardrail_events=rec.guardrail_events, hitl_decision=rec.hitl_decision, approvals=rec.approvals,
            latency_ms=round((time.perf_counter() - rec.started) * 1000, 1), versions=versions, **kw,
        )

    # 1. Input guardrail — before the agent or model exists.
    decision = check_input(question, policy)
    rec.guardrail(decision)
    if decision.blocked:
        return result(answer=policy.input_refusal, status="blocked", blocked=decision.blocked_payload())

    # 2. Agent loop with the tool guardrail in front of every tool.
    check_ctx = ToolCheckContext(policy=policy, index=index, specs=specs,
                                 known_recipients=ctx.crm.known_emails(), sandbox=ctx.sandbox)
    agent = build_agent(
        model or make_chat_model(settings),
        build_tools(specs, ctx),
        prompt.text,
        middleware=[ToolGuardrailMiddleware(check_ctx, rec)],
        checkpointer=InMemorySaver(),  # required to pause and resume at the approval gate
    )
    config = {"configurable": {"thread_id": rec.run_id}, "recursion_limit": settings.recursion_limit}
    try:
        state = agent.invoke({"messages": [{"role": "user", "content": question}]}, config)
        while state.get("__interrupt__"):
            interrupts = state["__interrupt__"]
            decisions = {i.id: _decide(i.value, approval, approver) for i in interrupts}
            resume = next(iter(decisions.values())) if len(decisions) == 1 else decisions
            state = agent.invoke(Command(resume=resume), config)
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
    status = "rejected" if rec.hitl_decision == "reject" else "completed"
    return result(answer=answer, status=status, **cost)
