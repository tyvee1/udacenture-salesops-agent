"""Run orchestration: the one function every entry point calls to ask the agent a question.

The CLI, the evaluation suite and the UI all go through :func:`ask` (and the UI
also through :func:`resume`), so every path gets the same configuration,
prompt, tools, runtime controls, state and logging:

1. **Input guardrail** — screens the request. A blocked request never reaches the
   agent; no model is even constructed, so no tokens are spent and no key is needed.
2. **Agent loop** — the model proposes tool calls; the tool guardrail middleware
   checks each one before it runs. Conversation memory is loaded from SQLite for
   this user/session/thread only.
3. **Approval gate** — when the agent pauses before a high-risk action, the
   decision comes from ``approval`` (the ``--approval`` flag), else from an
   interactive ``approver`` callback. With neither, the run either fails closed
   (``reject``) or, for the UI, returns ``awaiting_approval`` so a person can
   decide and :func:`resume` the same run.
4. **Output guardrail** — screens the final answer before it is returned.
5. **Logging** — whatever happened, one line goes to ``logs/runs.jsonl``, one trace
   to ``traces/<run_id>.json`` (both redacted) and one row to the run history.

Optional external tracing (``tracer``) wraps steps 1–5: one Langfuse trace per run,
with every guardrail and approval decision on it. See :mod:`salesops_agent.external_tracing`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain_core.messages import AIMessage
from langgraph.types import Command

from salesops_agent.agent import build_agent
from salesops_agent.config import ConfigError, Settings, load_settings, make_chat_model
from salesops_agent.costs import collect_usage, estimate_cost_usd
from salesops_agent.external_tracing import NullTracer, RunInfo
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
from salesops_agent.state import Identity, StateStore
from salesops_agent.tools import build_context, build_tools, load_registry
from salesops_agent.versioning import component_versions

#: Called with the pending action when the agent pauses; returns "approve" or "reject".
Approver = Callable[[dict[str, Any]], str]


@dataclass
class RunResult:
    """What a caller gets back from one question."""

    run_id: str
    answer: str
    status: str  # completed | blocked | rejected | error | awaiting_approval
    tools_called: list[str] = field(default_factory=list)
    tools_executed: list[str] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    blocked: dict[str, Any] | None = None
    hitl_decision: str | None = None
    approvals: list[dict[str, Any]] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    usage_is_estimate: bool = False
    failure_reason: str | None = None
    versions: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)
    trace_path: str | None = None


def _final_text(messages: list[Any]) -> str:
    """Text of the last AI message that is an answer rather than a tool request."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


class AgentRun:
    """One invocation, from input check to log line. :func:`ask` and :func:`resume` drive it."""

    def __init__(self, question: str, settings: Settings, model: Any, approval: str | None,
                 approver: Approver | None, defer_approval: bool, task_id: str | None,
                 user_id: str | None, session_id: str | None, thread_id: str | None,
                 callbacks: list[Any] | None = None, tracer: Any = None, source: str = "cli"):
        self.question, self.settings, self.model = question, settings, model
        self.approval, self.approver, self.defer_approval = approval, approver, defer_approval
        self.callbacks = callbacks or []
        self.tracer = tracer or NullTracer()
        self.source = source
        self.named_session = session_id  # sessions are only meaningful when the caller names one
        self.timestamp = utc_now()
        self.rec = RunRecorder(run_id=uuid.uuid4().hex[:12])
        self.rec.listeners.append(self.tracer.on_event)
        self.identity = Identity.create(user_id, session_id, thread_id, self.rec.run_id)
        self.prompt = load_prompt(settings)
        self.policy = load_policy(settings.root / "config" / "policy.yaml")
        self.specs = load_registry(settings.path("tool_registry"))
        self.ctx = build_context(settings)
        self.index = SensitiveIndex.build(self.ctx.restricted)
        self.versions = component_versions(settings, self.prompt)
        self.store = StateStore(settings.path("state_dir"))
        self.redactor = Redactor(
            self.policy, self.index,
            denied_tools={s.name for s in self.specs.values() if s.data_access in self.policy.denied_data_access},
        )
        self.context = {
            "task_id": task_id,
            "model": (f"scripted ({getattr(model, 'model_name', 'scripted')})" if model is not None
                      else f"{settings.model.provider}:{settings.model.name}"),
            "user_id": self.identity.user_id,
            "session_id": self.identity.session_id,
            "thread_id": self.identity.thread_id,
        }
        self.agent: Any = None
        self.config: dict[str, Any] = {}
        self.prior_messages = 0
        self.proposed: list[str] = []
        self.raw_answer: str | None = None
        self.pending: list[Any] = []
        self.finished: RunResult | None = None

    # ------------------------------------------------------------------ helpers

    def _result(self, **kw: Any) -> RunResult:
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
            identity={"user_id": self.identity.user_id, "session_id": self.identity.session_id,
                      "thread_id": self.identity.thread_id},
            **kw,
        )

    def _decide(self, pending: dict[str, Any]) -> dict[str, str] | None:
        """Decision for one paused action, or None to hand it back to the caller (UI)."""
        if self.approval is not None:
            return {"decision": self.approval, "source": "cli_flag"}
        if self.approver is not None:
            return {"decision": self.approver(pending), "source": "interactive"}
        if self.defer_approval:
            return None
        return {"decision": "reject", "source": "default_no_signal"}

    # ---------------------------------------------------------------- lifecycle

    def _run_info(self) -> RunInfo:
        return RunInfo(run_id=self.rec.run_id, question=self.question, user_id=self.identity.user_id,
                       session_id=self.named_session, thread_key=self.identity.thread_key,
                       prompt_version=self.prompt.version, model=self.context["model"],
                       task_id=self.context["task_id"], source=self.source)

    def _callbacks(self) -> list[Any]:
        return self.callbacks + self.tracer.callbacks()

    def start(self) -> RunResult:
        """Run from the input guardrail until the run finishes or pauses for a deferred approval."""
        with self.tracer.trace(self._run_info()):
            result = self._start()
            self.tracer.end(result)
            return result

    def _start(self) -> RunResult:
        rec, policy = self.rec, self.policy
        rec.event("run_started", input_chars=len(self.question), thread_key=self.identity.thread_key)

        decision = check_input(self.question, policy)
        rec.guardrail(decision)
        if decision.blocked:
            return self._finish(self._result(answer=policy.input_refusal, status="blocked",
                                             blocked=decision.blocked_payload()))
        try:
            model = self.model or make_chat_model(self.settings)
        except ConfigError as exc:
            rec.error(f"ConfigError: {exc}")
            return self._finish(self._result(answer="The agent is not configured.", status="error",
                                             failure_reason=f"configuration error: {exc}"))

        check_ctx = ToolCheckContext(policy=policy, index=self.index, specs=self.specs,
                                     known_recipients=self.ctx.crm.known_emails(), sandbox=self.ctx.sandbox)
        self.agent = build_agent(model, build_tools(self.specs, self.ctx), self.prompt.text,
                                 middleware=[ToolGuardrailMiddleware(check_ctx, rec)],
                                 checkpointer=self.store.checkpointer)
        self.config = {"configurable": {"thread_id": self.identity.thread_key},
                       "recursion_limit": self.settings.recursion_limit, "callbacks": self._callbacks()}
        self.prior_messages = len(self.store.thread_messages(self.identity))
        rec.event("memory_loaded", prior_messages=self.prior_messages)
        return self._drive({"messages": [{"role": "user", "content": self.question}]})

    def resume(self, decision: str) -> RunResult:
        """Apply a person's decision to every pending approval and continue (UI path)."""
        if not self.pending:
            raise RuntimeError("this run is not waiting for approval")
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        verdicts = {i.id: {"decision": decision, "source": "ui"} for i in self.pending}
        self.pending = []
        resume = next(iter(verdicts.values())) if len(verdicts) == 1 else verdicts
        # A resume after a person decides is a second trace in the same session.
        with self.tracer.trace(self._run_info()):
            self.config["callbacks"] = self._callbacks()
            result = self._drive(Command(resume=resume))
            self.tracer.end(result)
            return result

    def _drive(self, payload: Any) -> RunResult:
        """Invoke the agent, resolving approval pauses until it finishes or must wait for a person."""
        try:
            state = self.agent.invoke(payload, self.config)
            while state.get("__interrupt__"):
                interrupts = state["__interrupt__"]
                verdicts = {i.id: self._decide(i.value) for i in interrupts}
                if any(v is None for v in verdicts.values()):
                    self.pending = list(interrupts)
                    return self._result(answer="", status="awaiting_approval",
                                        pending_approvals=[i.value for i in interrupts])
                resume = next(iter(verdicts.values())) if len(verdicts) == 1 else verdicts
                state = self.agent.invoke(Command(resume=resume), self.config)
        except Exception as exc:  # model/provider/recursion failures become a recorded error, not a crash
            self.rec.error(f"{type(exc).__name__}: {exc}")
            return self._finish(self._result(answer="The agent failed to complete this request.", status="error",
                                             failure_reason=f"{type(exc).__name__}: {exc}"))
        return self._complete(state)

    def _complete(self, state: dict[str, Any]) -> RunResult:
        messages = state["messages"][self.prior_messages:]  # this run only; earlier turns are memory
        self.proposed.extend(tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls)
        usage = collect_usage(messages)
        cost = dict(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    estimated_cost_usd=estimate_cost_usd(usage, self.settings.model.name, self.settings.pricing),
                    usage_is_estimate=usage.estimated)
        self.raw_answer = _final_text(messages)
        out = check_output(self.raw_answer, self.policy, self.index)
        self.rec.guardrail(out)
        if out.blocked:
            return self._finish(self._result(answer=self.policy.output_refusal, status="blocked",
                                             blocked=out.blocked_payload(), **cost))
        status = "rejected" if self.rec.hitl_decision == "reject" else "completed"
        return self._finish(self._result(answer=self.raw_answer, status=status, **cost))

    def _finish(self, result: RunResult) -> RunResult:
        """Write the redacted log line, trace file and history row; close the state store."""
        self.rec.event("run_finished", status=result.status)
        kw = dict(question=self.question, timestamp=self.timestamp, redactor=self.redactor, context=self.context)
        entry = build_log_entry(result, self.rec, **kw)
        trace = build_trace(result, self.rec, raw_answer=self.raw_answer, **kw)
        path = write_run_artifacts(self.settings.path("logs_dir"), self.settings.path("traces_dir"), entry, trace)
        self.store.record_run(entry)
        self.store.close()
        result.trace_path = str(path.relative_to(self.settings.root))
        self.finished = result
        return result


def start_run(
    question: str,
    settings: Settings | None = None,
    model: Any = None,
    approval: str | None = None,
    approver: Approver | None = None,
    defer_approval: bool = False,
    task_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    callbacks: list[Any] | None = None,
    tracer: Any = None,
    source: str = "cli",
) -> tuple[AgentRun, RunResult]:
    """Start a run and return it with its first result (which may be ``awaiting_approval``)."""
    if approval is not None and approval not in ("approve", "reject"):
        raise ValueError("approval must be 'approve' or 'reject'")
    run = AgentRun(question, settings or load_settings(), model, approval, approver, defer_approval, task_id,
                   user_id, session_id, thread_id, callbacks, tracer, source)
    return run, run.start()


def ask(question: str, settings: Settings | None = None, model: Any = None, approval: str | None = None,
        approver: Approver | None = None, task_id: str | None = None, user_id: str | None = None,
        session_id: str | None = None, thread_id: str | None = None,
        callbacks: list[Any] | None = None, tracer: Any = None, source: str = "cli") -> RunResult:
    """Answer one SalesOps question under the runtime controls, and log it.

    Args:
        question: the user's request.
        settings: loaded configuration (defaults to the project's agent.yaml).
        model: optional chat model override; the configured provider is used when omitted.
        approval: ``"approve"`` or ``"reject"`` applied to every paused high-risk action.
        approver: interactive callback used when ``approval`` is not given.
        task_id: evaluation task id, recorded in the log when the run comes from the suite.
        user_id, session_id, thread_id: conversation identity; omit all three for a one-off question.
        callbacks: extra LangChain callback handlers.
        tracer: optional external tracer (see :mod:`salesops_agent.external_tracing`).
        source: where the run came from (``cli``, ``eval``, ``ui``), recorded as a trace tag.
    """
    _run, result = start_run(question, settings, model, approval, approver, False, task_id,
                             user_id, session_id, thread_id, callbacks, tracer, source)
    return result


def resume(run: AgentRun, decision: str) -> RunResult:
    """Continue a run that returned ``awaiting_approval`` with a person's decision."""
    return run.resume(decision)
