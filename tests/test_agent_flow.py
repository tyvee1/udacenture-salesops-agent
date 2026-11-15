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


def test_empty_analysis_result_reaches_the_model_with_its_note(project) -> None:
    """Regression for a live run: the model filtered on 'Customer' (the data says 'customer'),
    received [], and answered that no customers matched. The note must survive the guardrail
    middleware and arrive in the tool message the model reads next, and the trace must record it."""
    import json as _json

    from salesops_agent.tools.analysis import EMPTY_RESULT_NOTE

    code = ("result = [a for a in accounts if a['health_score'] is not None "
            "and a['health_score'] < 50 and a['account_status'] == 'Customer']")
    model = ScriptedChatModel(turns=[tool_call("analyze_crm_data", {"code": code}), answer("checked")])
    result = ask("Use the analysis tool to list customer accounts with a health score under 50.",
                 settings=project, model=model)

    seen_by_model = " ".join(str(m.content) for m in model.calls[-1])
    assert EMPTY_RESULT_NOTE in seen_by_model
    trace = _json.loads((project.root / "traces" / f"{result.run_id}.json").read_text())
    (call,) = trace["tool_calls"]
    assert call["status"] == "executed" and call["output"]["result"] == []
    assert call["output"]["note"] == EMPTY_RESULT_NOTE
