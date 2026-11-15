"""The evaluation suite's own checks: the task file, the expectation checker and the report.

A checker that cannot fail proves nothing, so each expectation type is shown
both rejecting a wrong run and accepting a right one.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from salesops_agent.evaluation.report import summarize
from salesops_agent.evaluation.runner import check_expectations
from salesops_agent.evaluation.tasks import EvalTask, load_tasks

ROOT = Path(__file__).resolve().parents[1]
TASKS = load_tasks(ROOT / "evals" / "tasks.yaml")

#: The rubric's ten required scenario categories.
RUBRIC_CATEGORIES = {
    "normal_salesops_question", "account_risk_reasoning", "pipeline_opportunity_reasoning",
    "restricted_input_blocked", "forbidden_tool_call_blocked", "sensitive_output_blocked",
    "sandbox_allowed_analysis", "sandbox_denied_unsafe_execution", "approved_email_sending",
    "rejected_email_sending",
}


def test_every_rubric_category_has_a_task() -> None:
    assert RUBRIC_CATEGORIES <= {t.category for t in TASKS}


def test_task_categories_are_all_known() -> None:
    assert {t.category for t in TASKS} <= RUBRIC_CATEGORIES


def test_unknown_expectation_key_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "tasks.yaml"
    bad.write_text("tasks:\n  - {id: t, category: c, input: q, expect: {statuss: completed}}\n")
    with pytest.raises(ValueError, match="unknown expectation"):
        load_tasks(bad)


def test_expected_entity_names_exist_in_the_data() -> None:
    """Names a task expects in an answer must come from the CRM data, not from memory."""
    contacts = json.loads((ROOT / "data" / "contacts.json").read_text())
    full_names = " ".join(f"{c['first_name']} {c['last_name']}" for c in contacts)
    corpus = (ROOT / "data" / "accounts.json").read_text() + " " + full_names
    for task in TASKS:
        if task.category in {"normal_salesops_question", "account_risk_reasoning"}:
            for name in task.expect.get("answer_contains_any", []):
                assert name in corpus, f"{task.id}: expected text {name!r} is not in the CRM data"


def _task(**expect) -> EvalTask:
    return EvalTask(id="t", category="c", description="", input="q", model="scripted", script=[], expect=expect)


def _run(**kw) -> SimpleNamespace:
    base = dict(status="completed", answer="", tools_called=[], tools_executed=[], guardrail_events=[],
                hitl_decision=None)
    return SimpleNamespace(**{**base, **kw})


CASES = [
    # (expectation, a run that must FAIL it, a run that must PASS it)
    ({"status": "blocked"}, _run(status="completed"), _run(status="blocked")),
    ({"blocked_layer": "tool"},
     _run(guardrail_events=[{"layer": "input", "action": "blocked"}]),
     _run(guardrail_events=[{"layer": "tool", "action": "blocked"}])),
    ({"blocked_reason": "x"},
     _run(guardrail_events=[{"reason": "x", "action": "allowed"}]),
     _run(guardrail_events=[{"reason": "x", "action": "blocked"}])),
    ({"tools_called_include": ["a"]}, _run(tools_called=["b"]), _run(tools_called=["a"])),
    ({"tools_executed_include": ["a"]}, _run(tools_executed=[]), _run(tools_executed=["a"])),
    ({"tools_not_executed": ["a"]}, _run(tools_executed=["a"]), _run(tools_executed=["b"])),
    ({"answer_contains_any": ["Acme"]}, _run(answer="nothing"), _run(answer="Acme Corp")),
    ({"answer_not_contains": ["$2M"]}, _run(answer="bonus $2M"), _run(answer="restricted")),
    ({"hitl_decision": "reject"}, _run(hitl_decision="approve"), _run(hitl_decision="reject")),
]


@pytest.mark.parametrize("expect,bad,good", CASES, ids=[next(iter(c[0])) for c in CASES])
def test_each_expectation_fires_and_stays_silent(expect, bad, good) -> None:
    assert check_expectations(_task(**expect), bad, 0), "checker failed to fire"
    assert check_expectations(_task(**expect), good, 0) == [], "checker fired on a correct run"


def test_outbox_delta_fires_and_stays_silent() -> None:
    task = _task(outbox_delta=0)
    assert check_expectations(task, _run(), 1)
    assert check_expectations(task, _run(), 0) == []


def test_report_summary_is_computed_from_rows() -> None:
    rows = [
        {"category": "a", "success": True, "latency_ms": 10.0, "estimated_cost_usd": 0.001, "usage_is_estimate": False, "mode": "live"},
        {"category": "a", "success": False, "latency_ms": 30.0, "estimated_cost_usd": 0.002, "usage_is_estimate": True, "mode": "scripted"},
    ]
    s = summarize(rows)
    assert (s["tasks"], s["passed"], s["failed"], s["pass_rate"]) == (2, 1, 1, 0.5)
    assert s["avg_latency_ms"] == 20.0 and s["total_estimated_cost_usd"] == pytest.approx(0.003)
    assert s["categories"] == {"a": {"tasks": 2, "passed": 1}} and s["any_usage_estimated"]
    json.dumps(s)  # serializable
