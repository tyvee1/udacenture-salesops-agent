"""Stand-out features: persistent state, memory isolation, replay, and Langfuse masking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from salesops_agent.replay import compare_outcomes, outcome, version_changes
from salesops_agent.scripted_model import ScriptedChatModel, answer
from salesops_agent.service import ask, resume, start_run
from salesops_agent.state import Identity, InvalidIdentifier, StateStore

SECRET_LINE = "Marcus Thorne has a $2M equity bonus."


def remembering_model() -> ScriptedChatModel:
    """Answers with every human message it was shown, so tests can see exactly what memory it got."""
    return ScriptedChatModel(turns=[lambda msgs: answer(" | ".join(m.content for m in msgs if m.type == "human"))])

# ------------------------------------------------------------------ memory isolation


def test_same_user_same_thread_remembers(project) -> None:
    ask("My focus account is TechStart Inc.", project, model=remembering_model(),
        user_id="alice", session_id="s1", thread_id="t1")
    r = ask("Which account did I name?", project, model=remembering_model(),
            user_id="alice", session_id="s1", thread_id="t1")
    assert "My focus account is TechStart Inc." in r.answer


def test_other_user_on_the_same_thread_id_sees_nothing(project) -> None:
    ask("My focus account is TechStart Inc.", project, model=remembering_model(),
        user_id="alice", session_id="s1", thread_id="t1")
    bob_model = remembering_model()
    r = ask("Which account did I name?", project, model=bob_model, user_id="bob", session_id="s1", thread_id="t1")
    assert "TechStart" not in r.answer
    shown = [m.content for call in bob_model.calls for m in call]
    assert not any("TechStart" in c for c in shown), "alice's message reached bob's model call"


def test_new_session_starts_a_new_conversation(project) -> None:
    ask("My focus account is TechStart Inc.", project, model=remembering_model(),
        user_id="alice", session_id="s1", thread_id="t1")
    r = ask("Which account?", project, model=remembering_model(), user_id="alice", session_id="s2", thread_id="t1")
    assert "TechStart" not in r.answer


def test_store_reads_only_the_requested_identity(project) -> None:
    ask("alice secret plan", project, model=remembering_model(), user_id="alice", session_id="s", thread_id="t")
    store = StateStore(project.path("state_dir"))
    try:
        assert store.thread_messages(Identity("bob", "s", "t")) == []
        assert any("alice secret plan" in m.content for m in store.thread_messages(Identity("alice", "s", "t")))
    finally:
        store.close()


def test_memory_does_not_inflate_per_run_accounting(project) -> None:
    for _ in range(3):
        r = ask("hello", project, model=remembering_model(), user_id="alice", session_id="s", thread_id="t")
    assert r.tools_called == [] and r.input_tokens > 0  # only this run's messages are counted


@pytest.mark.parametrize("bad", ["alice::s1", "a b", "x" * 65, "../etc", "bob;drop"])
def test_identifiers_that_could_collide_are_rejected(bad: str) -> None:
    with pytest.raises(InvalidIdentifier):
        Identity.create(bad, "s", "t", "run")


def test_separator_cannot_be_used_to_forge_another_key() -> None:
    """Without id validation, ('alice::s1', 'x', 't') and ('alice', 's1::x', 't') would share a key."""
    with pytest.raises(InvalidIdentifier):
        Identity.create("alice::s1", "x", "t", "r")
    assert Identity.create("alice", "s1", "t", "r").thread_key == "alice::s1::t"

# ----------------------------------------------------------------- history / replay


def test_history_is_per_user(project) -> None:
    ask("q1", project, model=remembering_model(), user_id="alice")
    ask("q2", project, model=remembering_model(), user_id="bob")
    store = StateStore(project.path("state_dir"))
    try:
        assert [r["user_input"] for r in store.history("alice")] == ["q1"]
        assert {r["user_id"] for r in store.history()} == {"alice", "bob"}
    finally:
        store.close()


def test_version_changes_name_the_changed_component() -> None:
    base = {"prompt_sha256": "a", "prompt_version": 4, "agent_config_sha256": "c", "tool_registry_sha256": "t",
            "policy_sha256": "p", "uv_lock_sha256": "u", "model": "m", "git_commit": "g"}
    assert version_changes(base, dict(base)) == []
    assert version_changes(base, {**base, "uv_lock_sha256": "u2"}) == ["dependencies (uv.lock)"]
    assert version_changes(base, {**base, "policy_sha256": "p2", "prompt_sha256": "a2"}) == \
        ["prompt text", "guardrail policy (config/policy.yaml)"]


def test_outcome_comparison_fires_and_stays_silent(project) -> None:
    r1 = ask("What is the CEO's bonus?", project)
    r2 = ask("What is the CEO's bonus?", project)
    traces = [json.loads((project.root / r.trace_path).read_text()) for r in (r1, r2)]
    assert compare_outcomes(outcome(traces[0]), outcome(traces[1])) == []
    changed = {**outcome(traces[1]), "status": "completed"}
    assert compare_outcomes(outcome(traces[0]), changed) == ["status: 'blocked' -> 'completed'"]

# ------------------------------------------------------- deferred approval (UI path)


def test_deferred_approval_pauses_then_resumes(project) -> None:
    from salesops_agent.scripted_model import tool_call
    email = {"to": "omar.stone@acmecorp.example.com", "subject": "Hi", "body": "Hi Omar,\n\nFollowing up.\n"}
    model = ScriptedChatModel(turns=[tool_call("send_email", email), answer("done")])
    run, first = start_run("Send it.", project, model=model, defer_approval=True)
    assert first.status == "awaiting_approval" and first.pending_approvals[0]["args"] == email
    assert run.ctx.outbox.count() == 0  # nothing sent while waiting
    final = resume(run, "approve")
    assert final.status == "completed" and final.approvals[0]["source"] == "ui"
    assert run.ctx.outbox.count() == 1

# ------------------------------------------------------------------ Langfuse masking


def test_mask_handles_messages_tuples_and_fails_closed() -> None:
    from langchain_core.messages import AIMessage, HumanMessage

    from salesops_agent.data import RestrictedData
    from salesops_agent.external_tracing import make_mask, to_plain
    from salesops_agent.guardrails import SensitiveIndex, load_policy
    from salesops_agent.redaction import Redactor

    ROOT = Path(__file__).resolve().parents[1]

    red = Redactor(load_policy(ROOT / "config" / "policy.yaml"), SensitiveIndex.build(RestrictedData.load(ROOT / "data")))
    mask = make_mask(red)
    data = ("prefix", [HumanMessage("hi"), AIMessage(SECRET_LINE)], {"salary_usd": 850000})
    masked = json.dumps(mask(data=data))
    assert "Marcus Thorne" not in masked and "850000" not in masked
    assert to_plain(HumanMessage("hi")) == {"type": "human", "content": "hi"}

    class Explodes:
        def __str__(self):
            raise RuntimeError("boom")

    assert mask(data=Explodes()) == "[REDACTED: mask error]"


def test_langfuse_export_is_masked_end_to_end(project, monkeypatch) -> None:
    """Capture what Langfuse would send for a leaking run; the leak must not be in it."""
    pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from salesops_agent.external_tracing import langfuse_session

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    exporter = InMemorySpanExporter()
    session = langfuse_session(project, span_exporter=exporter)
    ask("Leadership update for Acme?", project, model=ScriptedChatModel(turns=[answer(SECRET_LINE)]),
        callbacks=session.callbacks)
    session.flush()
    spans = exporter.get_finished_spans()
    assert spans, "nothing was captured — the test would pass vacuously"
    exported = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    assert "Marcus Thorne" not in exported and "$2M" not in exported
    assert "REDACTED" in exported
