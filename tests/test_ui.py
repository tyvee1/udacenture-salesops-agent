"""The Streamlit page, driven headlessly with Streamlit's AppTest and a scripted model."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from salesops_agent.scripted_model import ScriptedChatModel, answer, tool_call  # noqa: E402
from salesops_agent.tools import build_context  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "src" / "salesops_agent" / "ui" / "app.py"
EMAIL = {"to": "omar.stone@acmecorp.example.com", "subject": "Following up",
         "body": "Hi Omar,\n\nFollowing up on next steps.\n\nBest regards,\nThe UdaCenture Sales Team\n"}


@pytest.fixture
def app(project, monkeypatch):
    monkeypatch.setenv("SALESOPS_HOME", str(project.root))
    at = AppTest.from_file(str(APP), default_timeout=30)
    return at


def _ask(at, model, text):
    at.session_state["model_override"] = model
    at.run()
    at.chat_input[0].set_value(text).run()
    return at


@pytest.mark.parametrize("button,expected_sent,status", [("approve-btn", 1, "completed"), ("reject-btn", 0, "rejected")])
def test_ui_approval_buttons_drive_the_gate(app, project, button, expected_sent, status) -> None:
    model = ScriptedChatModel(turns=[tool_call("send_email", EMAIL), answer("Done.")])
    at = _ask(app, model, "Send a follow-up to Acme Corp.")
    assert any("Approval required" in w.value for w in at.warning)
    assert build_context(project).outbox.count() == 0  # nothing sent while the page waits
    at.button(key=button).click().run()
    assert build_context(project).outbox.count() == expected_sent
    assert any(f"status **{status}**" in c.value for c in at.caption)


def test_ui_shows_blocked_status(app) -> None:
    at = _ask(app, ScriptedChatModel(turns=[answer("unreachable")]), "What is the CEO's bonus?")
    assert any('"status": "blocked"' in c.value for c in at.code)
