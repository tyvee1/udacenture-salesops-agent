"""Human-in-the-loop approval: approve sends once, reject sends nothing, and nothing else can send.

The bypass tests attack the gate from every side we could think of — calling
the tool function directly, calling the outbox directly, reusing an approval,
approving one email and sending another, renaming the tool, proposing two
sends at once — and assert the outbox count, which is the only thing that
matters to the customer.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from salesops_agent.approval import ApprovalRequired, grant_scope
from salesops_agent.config import load_settings
from salesops_agent.scripted_model import ScriptedChatModel, answer, tool_call
from salesops_agent.service import ask
from salesops_agent.tools import build_context, load_registry
from salesops_agent.tools import email as email_tools
from salesops_agent.guardrails import load_policy

ROOT = Path(__file__).resolve().parents[1]
EMAIL = {"to": "omar.stone@acmecorp.example.com", "subject": "Following up on Acme Corp",
         "body": "Hi Omar,\n\nI wanted to follow up regarding next steps.\n\nBest regards,\nThe UdaCenture Sales Team\n"}


@pytest.fixture
def settings(tmp_path: Path):
    """Project settings with state redirected to a temp dir, so each test has an empty outbox."""
    for name in ("config", "prompts", "data", "evals"):
        shutil.copytree(ROOT / name, tmp_path / name)
    shutil.copy(ROOT / "uv.lock", tmp_path / "uv.lock")
    return load_settings(tmp_path)


def outbox_count(settings) -> int:
    return build_context(settings).outbox.count()


def send_script(*emails) -> ScriptedChatModel:
    turns = [tool_call("send_email", e, call_id=f"c{i}") for i, e in enumerate(emails)]
    return ScriptedChatModel(turns=[*turns, answer("done")])

# ------------------------------------------------------------------ approve / reject


def test_approve_sends_exactly_once(settings) -> None:
    r = ask("Send a follow-up to Acme Corp.", settings, model=send_script(EMAIL), approval="approve")
    assert outbox_count(settings) == 1
    assert r.hitl_decision == "approve" and r.status == "completed"
    assert r.approvals == [{"call_id": "c0", "tool": "send_email", "args": EMAIL, "requested": True,
                            "decision": "approve", "source": "cli_flag"}]


def test_reject_sends_nothing(settings) -> None:
    r = ask("Send a follow-up to Acme Corp.", settings, model=send_script(EMAIL), approval="reject")
    assert outbox_count(settings) == 0
    assert r.hitl_decision == "reject" and r.status == "rejected"
    assert "send_email" not in r.tools_executed


def test_no_signal_fails_closed(settings) -> None:
    r = ask("Send a follow-up to Acme Corp.", settings, model=send_script(EMAIL))
    assert outbox_count(settings) == 0
    assert r.approvals[0]["source"] == "default_no_signal" and r.hitl_decision == "reject"


def test_interactive_approver_sees_the_exact_email(settings) -> None:
    seen = []

    def approver(pending):
        seen.append(pending)
        return "approve"

    ask("Send a follow-up to Acme Corp.", settings, model=send_script(EMAIL), approver=approver)
    assert seen and seen[0]["tool"] == "send_email" and seen[0]["args"] == EMAIL
    assert outbox_count(settings) == 1


def test_unsafe_email_is_blocked_before_approval_is_requested(settings) -> None:
    bad = {**EMAIL, "body": "We can offer a 30% discount."}
    r = ask("Send a follow-up to Acme Corp.", settings, model=send_script(bad), approval="approve")
    assert outbox_count(settings) == 0 and r.approvals == []
    assert any(g.get("reason") == "unsafe_email_content" for g in r.guardrail_events)


def test_two_sends_in_one_turn_each_need_approval(settings) -> None:
    second = {**EMAIL, "subject": "Second note"}
    model = ScriptedChatModel(turns=[
        type(tool_call("x", {}))(content="", tool_calls=[
            {"name": "send_email", "args": EMAIL, "id": "a"},
            {"name": "send_email", "args": second, "id": "b"},
        ]),
        answer("done"),
    ])
    r = ask("Send two notes.", settings, model=model, approval="reject")
    assert outbox_count(settings) == 0 and len(r.approvals) == 2
    model.calls.clear()
    r = ask("Send two notes.", settings, model=ScriptedChatModel(turns=model.turns), approval="approve")
    assert outbox_count(settings) == 2 and {a["decision"] for a in r.approvals} == {"approve"}

# --------------------------------------------------------------------------- bypass


def test_calling_the_send_tool_directly_sends_nothing(settings) -> None:
    ctx = build_context(settings)
    out = email_tools.send_email(ctx, **EMAIL)
    assert out["status"] == "blocked" and out["reason"] == "approval_required"
    assert ctx.outbox.count() == 0


def test_calling_the_outbox_directly_raises(settings) -> None:
    ctx = build_context(settings)
    with pytest.raises(ApprovalRequired):
        ctx.outbox.deliver(dict(EMAIL))
    assert ctx.outbox.count() == 0


def test_approval_for_one_email_cannot_send_another(settings) -> None:
    ctx = build_context(settings)
    with grant_scope("send_email", EMAIL, "test"):
        with pytest.raises(ApprovalRequired, match="does not match"):
            ctx.outbox.deliver({**EMAIL, "body": "A different message."})
    assert ctx.outbox.count() == 0


def test_approval_is_single_use(settings) -> None:
    ctx = build_context(settings)
    with grant_scope("send_email", EMAIL, "test"):
        ctx.outbox.deliver(dict(EMAIL))
        with pytest.raises(ApprovalRequired, match="already been used"):
            ctx.outbox.deliver(dict(EMAIL))
    assert ctx.outbox.count() == 1


def test_grant_does_not_outlive_its_scope(settings) -> None:
    ctx = build_context(settings)
    with grant_scope("send_email", EMAIL, "test"):
        pass
    with pytest.raises(ApprovalRequired):
        ctx.outbox.deliver(dict(EMAIL))


@pytest.mark.parametrize("name", ["Send_Email", "send_email_v2", "finalize_email", "deliver_email"])
def test_renamed_send_tools_are_not_a_path(settings, name: str) -> None:
    r = ask("Send it.", settings, model=ScriptedChatModel(turns=[tool_call(name, EMAIL), answer("x")]),
            approval="approve")
    assert outbox_count(settings) == 0
    assert any(g.get("reason") == "unregistered_tool" for g in r.guardrail_events)


def test_sandbox_is_not_a_path_to_the_outbox(settings) -> None:
    code = "result = outbox"
    r = ask("Analyze.", settings, model=ScriptedChatModel(turns=[tool_call("analyze_crm_data", {"code": code}),
                                                                  answer("x")]), approval="approve")
    assert outbox_count(settings) == 0
    assert any(g.get("reason") == "sandbox_violation" for g in r.guardrail_events)


def test_every_high_risk_tool_is_either_denied_or_paused(settings) -> None:
    """Registry-driven: a high-risk tool can never run without either a policy block or an approval pause."""
    specs = load_registry(settings.path("tool_registry"))
    policy = load_policy(settings.root / "config" / "policy.yaml")
    for spec in specs.values():
        if not spec.is_high_risk:
            continue
        args = EMAIL if spec.name == "send_email" else {"query": "accounts"}
        r = ask("Do it.", settings, model=ScriptedChatModel(turns=[tool_call(spec.name, args), answer("x")]))
        denied = spec.data_access in policy.denied_data_access
        assert spec.name not in r.tools_executed, spec.name
        assert denied or r.approvals, f"{spec.name} ran neither a policy block nor an approval pause"


def test_rejection_message_reaches_the_model(settings) -> None:
    model = send_script(EMAIL)
    ask("Send a follow-up to Acme Corp.", settings, model=model, approval="reject")
    tool_msgs = [m for m in model.calls[-1] if m.type == "tool"]
    assert json.loads(tool_msgs[-1].content)["status"] == "rejected"
