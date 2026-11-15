"""Structured logs, trace artifacts, redaction and the monitoring report.

Runs here go through the real service with a scripted model, then the files
on disk are read back — so these tests check what an operator would actually
open, not an in-memory object.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from salesops_agent.monitoring import aggregate, interpretation, render_markdown
from salesops_agent.observability import read_log
from salesops_agent.scripted_model import ScriptedChatModel, answer, tool_call
from salesops_agent.service import ask

ROOT = Path(__file__).resolve().parents[1]
LOG_SCHEMA = json.loads((ROOT / "schemas" / "run_log.schema.json").read_text())
TRACE_SCHEMA = json.loads((ROOT / "schemas" / "trace.schema.json").read_text())
EMAIL = {"to": "omar.stone@acmecorp.example.com", "subject": "Following up",
         "body": "Hi Omar,\n\nFollowing up on next steps.\n\nBest regards,\nThe UdaCenture Sales Team\n"}

#: Strings that must never reach disk: raw restricted values from the HR and financial files.
SECRETS = ["$2M", "equity bonus", "active negotiation", "850000", "850,000", "2000000",
           "15% reduction", "max_unapproved_discount", "Pending termination"]


def _files(settings) -> tuple[list[dict], dict[str, dict]]:
    rows = read_log(settings.path("logs_dir"))
    traces = {p.stem: json.loads(p.read_text()) for p in settings.path("traces_dir").glob("*.json")}
    return rows, traces


def _run_mix(settings) -> None:
    """One run of each kind: answered, input-blocked, tool-blocked, output-blocked, approved, rejected, error."""
    ask("Summarize the current sales pipeline by stage.", settings,
        model=ScriptedChatModel(turns=[tool_call("summarize_pipeline", {}), answer("Pipeline is healthy.")]),
        task_id="t-answer")
    ask("What is the CEO's bonus?", settings, model=ScriptedChatModel(turns=[answer("unreachable")]))
    ask("Anything internal on Acme?", settings, model=ScriptedChatModel(turns=[
        tool_call("lookup_internal_financial_data", {"query": "CONFIDENTIAL_M_AND_A"}), answer("Restricted.")]))
    ask("Leadership update for Acme?", settings,
        model=ScriptedChatModel(turns=[answer("Marcus Thorne has a $2M equity bonus.")]))
    ask("Send it.", settings, model=ScriptedChatModel(turns=[tool_call("send_email", EMAIL), answer("sent")]),
        approval="approve")
    ask("Send it.", settings, model=ScriptedChatModel(turns=[tool_call("send_email", EMAIL), answer("not sent")]),
        approval="reject")
    ask("Summarize TechStart.", settings, model=ScriptedChatModel(turns=[]))  # model with no turns -> error


def test_every_invocation_writes_one_log_line_and_one_trace(project) -> None:
    _run_mix(project)
    rows, traces = _files(project)
    assert len(rows) == 7 and len(traces) == 7
    assert {r["run_id"] for r in rows} == set(traces)
    assert {r["final_status"] for r in rows} == {"completed", "blocked", "rejected", "error"}


def test_log_lines_match_schema_and_carry_rubric_fields(project) -> None:
    _run_mix(project)
    rows, _ = _files(project)
    for row in rows:
        jsonschema.validate(row, LOG_SCHEMA)
    answered = next(r for r in rows if r["task_id"] == "t-answer")
    assert answered["tools_called"] == [{"tool": "summarize_pipeline", "status": "executed"}]
    assert answered["latency_ms"] > 0 and answered["estimated_cost_usd"] > 0


def test_traces_match_schema_and_carry_rubric_fields(project) -> None:
    _run_mix(project)
    _, traces = _files(project)
    for t in traces.values():
        jsonschema.validate(t, TRACE_SCHEMA)
    blocked_tool = next(t for t in traces.values() if "lookup_internal_financial_data" in t["selected_tools"])
    call = blocked_tool["tool_calls"][0]
    assert call["args"]["query"] and call["status"] == "blocked"
    assert call["output"] == {"status": "blocked", "reason": "restricted_internal_data",
                              "policy": "salesops_data_access_policy", "tool": "lookup_internal_financial_data",
                              "detail": "data_access=restricted_financial is not permitted for SalesOps requests"}


def test_trace_shows_approval_was_requested_and_decided(project) -> None:
    _run_mix(project)
    rows, traces = _files(project)
    for decision in ("approve", "reject"):
        row = next(r for r in rows if r["hitl_decision"] == decision)
        trace = traces[row["run_id"]]
        assert trace["hitl"]["approval_requested"] is True and trace["hitl"]["decision"] == decision
        kinds = [e["kind"] for e in trace["timeline"]]
        assert kinds.index("approval_requested") < kinds.index("approval_decided")
        assert row["hitl_approvals"] == [{"tool": "send_email", "decision": decision, "source": "cli_flag"}]


def test_error_runs_record_the_failure(project) -> None:
    _run_mix(project)
    rows, traces = _files(project)
    err = next(r for r in rows if r["final_status"] == "error")
    assert err["failure_reason"] and traces[err["run_id"]]["errors"]


def test_no_restricted_value_reaches_disk(project) -> None:
    _run_mix(project)
    blob = (project.path("logs_dir") / "runs.jsonl").read_text()
    blob += "".join(p.read_text() for p in project.path("traces_dir").glob("*.json"))
    leaks = [s for s in SECRETS if s.lower() in blob.lower()]
    assert not leaks, f"restricted values on disk: {leaks}"
    assert "[REDACTED: executive_compensation]" in blob  # the leak was recorded as redacted, not dropped silently


def test_ordinary_crm_values_are_not_redacted(project) -> None:
    ask("Pipeline?", project, model=ScriptedChatModel(turns=[tool_call("summarize_pipeline", {}), answer("ok")]))
    _, traces = _files(project)
    out = next(iter(traces.values()))["tool_calls"][0]["output"]
    assert isinstance(out["open_pipeline"]["amount_usd"], int)  # numbers in CRM output survive


def test_redaction_backstop_for_a_restricted_tool_output(project) -> None:
    """If a restricted tool ever did run, its output must still not reach the trace."""
    from salesops_agent.data import RestrictedData
    from salesops_agent.guardrails import SensitiveIndex, load_policy
    from salesops_agent.redaction import Redactor

    restricted = RestrictedData.load(project.path("data_dir"))
    red = Redactor(load_policy(project.root / "config" / "policy.yaml"), SensitiveIndex.build(restricted),
                   denied_tools={"lookup_internal_hr_data"})
    event = {"tool": "lookup_internal_hr_data", "args": {"query": "x"}, "status": "executed",
             "output": {"data": restricted.hr}}
    assert red.tool_event(event)["output"] == "[REDACTED: restricted tool output]"
    keyed = red.obj(restricted.hr)  # rule 2 on its own
    assert all(r.get("salary_usd") == "[REDACTED]" for r in keyed["records"])

# ------------------------------------------------------------------- monitoring report


def test_monitoring_report_aggregates_the_logged_runs(project) -> None:
    _run_mix(project)
    m = aggregate(read_log(project.path("logs_dir")))
    assert m["total_runs"] == 7
    # A tool-layer block does not end the run: the model answers without the data, so that run is "completed".
    assert m["status_counts"] == {"blocked": 2, "completed": 3, "error": 1, "rejected": 1}
    assert (m["success_count"], m["failure_count"]) == (6, 1)
    assert m["success_rate"] == round(6 / 7, 4) and m["failure_rate"] == round(1 / 7, 4)
    assert m["guardrail_blocks"]["by_layer"] == {"input": 1, "output": 1, "tool": 1}
    assert (m["hitl"]["approvals"], m["hitl"]["rejections"]) == (1, 1)
    assert m["tool_call_counts"]["send_email"] == 2
    assert m["evaluation_runs"] == 1 and m["common_failure_reasons"]


def test_report_prose_is_derived_from_the_numbers(project) -> None:
    _run_mix(project)
    m = aggregate(read_log(project.path("logs_dir")))
    text = "\n".join(interpretation(m))
    assert f"{m['success_count']} of {m['total_runs']} runs" in text
    assert f"{m['guardrail_blocks']['total']} blocks" in text
    md = render_markdown(m, "now")
    assert "| Total runs | 7 |" in md and "| HITL rejections | 1 |" in md


def test_degenerate_input_empty_log() -> None:
    m = aggregate([])
    assert m["total_runs"] == 0 and m["success_rate"] == 0.0
    assert "No runs have been logged yet" in interpretation(m)[0]


def test_malformed_log_lines_are_counted_not_fatal(project) -> None:
    logs = project.path("logs_dir")
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "runs.jsonl").write_text('{"final_status": "completed", "latency_ms": 5}\nnot json\n')
    m = aggregate(read_log(logs))
    assert m["total_runs"] == 1 and m["malformed_lines_skipped"] == 1
