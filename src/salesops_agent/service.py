"""Run orchestration: the one function every entry point calls to ask the agent a question.

The CLI, the evaluation suite and the UI all go through :func:`ask`, so every
path gets the same configuration, prompt, tools, runtime controls and logging:

1. **Input guardrail** — screens the request. A blocked request never reaches the
   agent; no model is even constructed, so no tokens are spent and no key is needed.
2. **Agent loop** — the model proposes tool calls; the tool guardrail middleware
   checks each one before it runs.
3. **Approval gate** — when the agent pauses before a high-risk action, the
   decision comes from ``approval`` (the ``--approval`` flag), else from an
   interactive ``approver`` callback, else it is ``reject``: a run with nobody to
   ask fails closed.
4. **Output guardrail** — screens the final answer before it is returned.
5. **Logging** — whatever happened, one line goes to ``logs/runs.jsonl`` and one
   trace to ``traces/<run_id>.json``, both redacted.
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
from salesops_agent.config import ConfigError, Settings, load_settings, make_chat_model
from salesops_agent.costs import collect_usage, estimate_cost_usd
from salesops_agent.guardrails import (
    SensitiveIndex,
    ToolCheckContext,
    ToolGuardrailMiddleware,
    check_input,
    check_output,
    load_policy,
)
from salesops_agent.observability import build_log_entry, build_trace, utc_now, write_run_artifacts
from salesops_agent.prompts import load_prompt
from salesops_agent.recorder import RunRecorder
from salesops_agent.redaction import Redactor
from salesops_agent.tools import build_context, build_tools, load_registry
from salesops_agent.versioning import component_versions

#: Called with the pending action when the agent pauses; returns "approve" or "reject".
Approver = Callable[[dict[str, Any]], str]


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
    trace_path: str | None = None


def _final_text(messages: list[Any]) -> str:
    """Text of the last AI message that is an answer rather than a tool request."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def _decide(pending: dict[str, Any], approval: str | None, approver: Approver | None) -> dict[str, str]:
    """Pick the decision for one paused action and say where it came from."""
    if approval is not None:
        return {"decision": approval, "source": "cli_flag"}
    if approver is not None:
        return {"decision": approver(pending), "source": "interactive"}
    return {"decision": "reject", "source": "default_no_signal"}


class _Run:
    """State for one invocation; :func:`ask` is its public face."""

    def __init__(self, question: str, settings: Settings, model: Any, approval: str | None,
                 approver: Approver | None, context: dict[str, Any]):
        self.question, self.settings, self.model = question, settings, model
        self.approval, self.approver, self.context = approval, approver, context
        self.timestamp = utc_now()
        self.rec = RunRecorder(run_id=uuid.uuid4().hex[:12])
        self.prompt = load_prompt(settings)
        self.policy = load_policy(settings.root / "config" / "policy.yaml")
        self.specs = load_registry(settings.path("tool_registry"))
        self.ctx = build_context(settings)
        self.index = SensitiveIndex.build(self.ctx.restricted)
        self.versions = component_versions(settings, self.prompt)
        self.proposed: list[str] = []
        self.raw_answer: str | None = None
        self.redactor = Redactor(
            self.policy, self.index,
            denied_tools={s.name for s in self.specs.values() if s.data_access in self.policy.denied_data_access},
        )

    def result(self, **kw: Any) -> RunResult:
        rec = self.rec
        return RunResult(
            run_id=rec.run_id,
            tools_called=list(self.proposed),
            tools_executed=[t["tool"] for t in rec.tool_events if t["status"] == "executed"],
            guardrail_events=rec.guardrail_events,
            hitl_decision=rec.hitl_decision,
            approvals=rec.approvals,
            latency_ms=round((time.perf_counter() - rec.started) * 1000, 1),
            versions=self.versions,
            **kw,
        )

    def execute(self) -> RunResult:
        """Run the controls and the agent. Never raises for a run failure; returns an error result."""
        rec, policy = self.rec, self.policy
        rec.event("run_started", input_chars=len(self.question))

        decision = check_input(self.question, policy)
        rec.guardrail(decision)
        if decision.blocked:
            return self.result(answer=policy.input_refusal, status="blocked", blocked=decision.blocked_payload())

        check_ctx = ToolCheckContext(policy=policy, index=self.index, specs=self.specs,
                                     known_recipients=self.ctx.crm.known_emails(), sandbox=self.ctx.sandbox)
        try:
            model = self.model or make_chat_model(self.settings)
        except ConfigError as exc:
            rec.error(f"ConfigError: {exc}")
            return self.result(answer="The agent is not configured.", status="error",
                               failure_reason=f"configuration error: {exc}")
        agent = build_agent(model, build_tools(self.specs, self.ctx), self.prompt.text,
                            middleware=[ToolGuardrailMiddleware(check_ctx, rec)],
                            checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": rec.run_id}, "recursion_limit": self.settings.recursion_limit}
        try:
            state = agent.invoke({"messages": [{"role": "user", "content": self.question}]}, config)
            while state.get("__interrupt__"):
                interrupts = state["__interrupt__"]
                decisions = {i.id: _decide(i.value, self.approval, self.approver) for i in interrupts}
                resume = next(iter(decisions.values())) if len(decisions) == 1 else decisions
                state = agent.invoke(Command(resume=resume), config)
        except Exception as exc:  # model/provider/recursion failures become a recorded error, not a crash
            rec.error(f"{type(exc).__name__}: {exc}")
            return self.result(answer="The agent failed to complete this request.", status="error",
                               failure_reason=f"{type(exc).__name__}: {exc}")

        messages = state["messages"]
        self.proposed.extend(tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
        usage = collect_usage(messages)
        cost = dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    estimated_cost_usd=estimate_cost_usd(usage, self.settings.model.name, self.settings.pricing),
                    usage_is_estimate=usage.estimated)

        self.raw_answer = _final_text(messages)
        out = check_output(self.raw_answer, policy, self.index)
        rec.guardrail(out)
        if out.blocked:
            return self.result(answer=policy.output_refusal, status="blocked", blocked=out.blocked_payload(), **cost)
        status = "rejected" if rec.hitl_decision == "reject" else "completed"
        return self.result(answer=self.raw_answer, status=status, **cost)

    def record(self, result: RunResult) -> RunResult:
        """Write the redacted log line and trace file for this run."""
        self.rec.event("run_finished", status=result.status)
        kw = dict(question=self.question, timestamp=self.timestamp, redactor=self.redactor, context=self.context)
        entry = build_log_entry(result, self.rec, **kw)
        trace = build_trace(result, self.rec, raw_answer=self.raw_answer, **kw)
        path = write_run_artifacts(self.settings.path("logs_dir"), self.settings.path("traces_dir"), entry, trace)
        result.trace_path = str(path.relative_to(self.settings.root))
        return result


def ask(
    question: str,
    settings: Settings | None = None,
    model: Any = None,
    approval: str | None = None,
    approver: Approver | None = None,
    task_id: str | None = None,
) -> RunResult:
    """Answer one SalesOps question under the runtime controls, and log it.

    Args:
        question: the user's request.
        settings: loaded configuration (defaults to the project's agent.yaml).
        model: optional chat model override; the configured provider is used when omitted.
        approval: ``"approve"`` or ``"reject"`` applied to every paused high-risk action.
        approver: interactive callback used when ``approval`` is not given.
        task_id: evaluation task id, recorded in the log when the run comes from the suite.
    """
    if approval is not None and approval not in ("approve", "reject"):
        raise ValueError("approval must be 'approve' or 'reject'")
    settings = settings or load_settings()
    model_label = (f"scripted ({getattr(model, 'model_name', 'scripted')})" if model is not None
                   else f"{settings.model.provider}:{settings.model.name}")
    run = _Run(question, settings, model, approval, approver,
               context={"task_id": task_id, "model": model_label})
    return run.record(run.execute())
