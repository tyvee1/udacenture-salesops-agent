"""End-to-end agent runs with a scripted model: real tools, real prompt, no network."""

from __future__ import annotations

from salesops_agent.scripted_model import ScriptedChatModel, answer, tool_call
from salesops_agent.service import ask


def test_ask_runs_a_tool_and_returns_the_final_answer(project) -> None:
    model = ScriptedChatModel(turns=[
        tool_call("summarize_pipeline", {}),
        lambda msgs: answer("Pipeline summary: " + msgs[-1].content[:60]),
    ])
    result = ask("Summarize the current sales pipeline by stage.", project, model=model)
    assert result.status == "completed"
    assert result.tools_called == ["summarize_pipeline"]
    assert result.answer.startswith("Pipeline summary:") and "by_stage" in result.answer
    assert result.versions["prompt_sha256"] and result.versions["uv_lock_sha256"]


def test_system_prompt_reaches_the_model(project) -> None:
    model = ScriptedChatModel(turns=[answer("ok")])
    ask("hello", project, model=model)
    first_call = model.calls[0]
    assert first_call[0].type == "system" and "SalesOps Agent for UdaCenture" in first_call[0].content
