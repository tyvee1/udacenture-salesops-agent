"""Langfuse export: the shape of each trace, checked from the spans the SDK would send.

Spans are captured with an in-memory OpenTelemetry exporter, so nothing leaves the
machine and no Langfuse keys are needed. Each test pins one property from Langfuse's
"what does a good trace look like" guidance, and each has a control that would fail
if the property were missing:

- one run is one trace, even when it pauses for approval and the agent runs twice;
- every guardrail and approval decision is on the trace, including a request the
  input guardrail blocks before any model runs;
- trace attributes (name, user, session, version, metadata) are set, and a session
  is only set when the caller named one;
- the system prompt reaches Langfuse intact while restricted values do not;
- a tracing failure never changes the outcome of the run.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from typing import Any

import pytest

pytest.importorskip("langfuse")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from salesops_agent.evaluation.tasks import load_tasks, script_to_model  # noqa: E402
from salesops_agent.external_tracing import (  # noqa: E402
    APPROVAL_NAME,
    APPROVAL_SCORE,
    ROOT_NAME,
    TRACE_NAME,
    langfuse_session,
)
from salesops_agent.prompts import load_prompt  # noqa: E402
from salesops_agent.scripted_model import ScriptedChatModel, answer, tool_call  # noqa: E402
from salesops_agent.service import ask  # noqa: E402

LEAK = "Marcus Thorne will receive a $2M retention bonus."


@pytest.fixture
def traced(project, monkeypatch):
    """A Langfuse session whose spans and scores are captured instead of sent."""
    # The SDK keeps one client per public key for the whole process; a key per test keeps
    # each test's exporter its own (otherwise every test after the first captures nothing).
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", f"pk-lf-test-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    exporter = InMemorySpanExporter()
    session = langfuse_session(project, span_exporter=exporter)
    scores: list[dict[str, Any]] = []
    monkeypatch.setattr(session.client, "score_current_trace", lambda **kw: scores.append(kw))

    def spans():
        session.flush()
        return exporter.get_finished_spans()

    return session, spans, scores


def task_model(project, task_id: str):
    tasks = {t.id: t for t in load_tasks(project.root / "evals" / "tasks.yaml")}
    return tasks[task_id].input, script_to_model(tasks[task_id])


def by_name(spans) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = defaultdict(list)
    for s in spans:
        out[s.name].append(s)
    return out


def attr(span, key: str) -> Any:
    value = (span.attributes or {}).get(key)
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


# ------------------------------------------------------------------ one run, one trace


def test_an_approved_email_run_is_one_trace_with_its_approval(project, traced) -> None:
    session, spans, scores = traced
    question, model = task_model(project, "email-approved")
    result = ask(question, project, model=model, approval="approve", tracer=session.tracer)
    captured = spans()

    assert result.status == "completed" and "send_email" in result.tools_executed
    assert len({s.context.trace_id for s in captured}) == 1, "the approval pause split the run into two traces"
    names = by_name(captured)
    assert len(names["LangGraph"]) == 2, "control: the agent really was invoked twice (before and after the pause)"
    (root,) = names[ROOT_NAME]
    assert root.parent is None and attr(root, "langfuse.observation.type") == "agent"
    assert attr(root, "langfuse.observation.output") == [{"role": "assistant", "content": result.answer}]
    (approval,) = names[APPROVAL_NAME]
    assert attr(approval, "langfuse.observation.type") == "guardrail"
    assert attr(approval, "langfuse.observation.output") == {"decision": "approve", "source": "cli_flag"}
    assert scores == [{"name": APPROVAL_SCORE, "value": 1, "data_type": "BOOLEAN",
                       "comment": "approve via cli_flag for send_email"}]
    assert {"check-input", "check-tool-call", "check-output"} <= set(names)


def test_a_rejected_email_is_scored_zero_and_marked(project, traced) -> None:
    session, spans, scores = traced
    question, model = task_model(project, "email-rejected")
    result = ask(question, project, model=model, approval="reject", tracer=session.tracer)
    names = by_name(spans())

    assert result.status == "rejected"
    (approval,) = names[APPROVAL_NAME]
    assert attr(approval, "langfuse.observation.level") == "WARNING"
    assert [s["value"] for s in scores] == [0]
    (root,) = names[ROOT_NAME]
    assert attr(root, "langfuse.observation.level") == "WARNING"


def test_a_blocked_request_still_produces_a_trace(project, traced) -> None:
    """Before this, an input-guardrail block sent nothing at all to Langfuse."""
    session, spans, _ = traced
    result = ask("What is the CEO's bonus this year?", project,
                 model=ScriptedChatModel(turns=[answer("unused")]), tracer=session.tracer)
    names = by_name(spans())

    assert result.status == "blocked"
    (check,) = names["check-input"]
    assert attr(check, "langfuse.observation.level") == "WARNING"
    assert attr(check, "langfuse.observation.output")["action"] == "blocked"
    assert "LangGraph" not in names, "control: the blocked request never reached the agent"
    (root,) = names[ROOT_NAME]
    assert attr(root, "langfuse.observation.status_message") == result.blocked["reason"]


# ------------------------------------------------------------------ attributes


def test_trace_attributes_identify_the_run(project, traced) -> None:
    session, spans, _ = traced
    model = ScriptedChatModel(turns=[tool_call("summarize_pipeline", {}), answer("Pipeline summarized.")])
    result = ask("Summarize the pipeline by stage.", project, model=model, user_id="ty", session_id="demo",
                 tracer=session.tracer)
    (root,) = by_name(spans())[ROOT_NAME]

    assert attr(root, "langfuse.trace.name") == TRACE_NAME
    assert attr(root, "user.id") == "ty" and attr(root, "session.id") == "demo"
    assert attr(root, "langfuse.version") == f"prompt-v{load_prompt(project).version}"
    assert attr(root, "langfuse.trace.metadata.run_id") == result.run_id
    assert attr(root, "langfuse.trace.metadata.local_trace") == f"traces/{result.run_id}.json"
    assert attr(root, "langfuse.environment") == "development"


def test_a_one_off_question_has_no_session(project, traced) -> None:
    """Negative control: a generated per-run session would make one Langfuse session per question."""
    session, spans, _ = traced
    ask("Summarize the pipeline by stage.", project,
        model=ScriptedChatModel(turns=[answer("ok")]), tracer=session.tracer)
    (root,) = by_name(spans())[ROOT_NAME]
    assert attr(root, "session.id") is None
    assert attr(root, "user.id") == "anonymous"


# ------------------------------------------------------------------ masking


def test_system_prompt_is_intact_and_restricted_values_are_not(project, traced) -> None:
    session, spans, _ = traced
    ask("Leadership update for Acme?", project, model=ScriptedChatModel(turns=[answer(LEAK)]),
        tracer=session.tracer)
    captured = spans()
    exported = json.dumps([dict(s.attributes or {}) for s in captured], default=str)

    assert "Marcus Thorne" not in exported and "$2M" not in exported
    assert "REDACTED" in exported, "control: the leak was present and was stopped"
    (generation,) = [s for s in captured if attr(s, "langfuse.observation.type") == "generation"]
    messages = attr(generation, "langfuse.observation.input")
    assert messages[0] == {"role": "system", "content": load_prompt(project).text}


# ------------------------------------------------------------------ fail open


def test_a_tracing_failure_does_not_change_the_run(project, traced, monkeypatch) -> None:
    session, _, _ = traced
    question, model = task_model(project, "email-approved")
    untraced = ask(question, project, model=task_model(project, "email-approved")[1], approval="approve")

    def broken(**_):
        raise RuntimeError("langfuse is down")

    monkeypatch.setattr(session.client, "start_as_current_observation", broken)
    monkeypatch.setattr(session.client, "score_current_trace", broken)
    traced_result = ask(question, project, model=model, approval="approve", tracer=session.tracer)

    assert traced_result.status == untraced.status == "completed"
    assert traced_result.tools_executed == untraced.tools_executed
    assert traced_result.answer == untraced.answer
