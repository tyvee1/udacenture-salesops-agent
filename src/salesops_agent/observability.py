"""Structured run logs and per-run trace artifacts.

Every invocation of :func:`salesops_agent.service.ask` — answered, blocked,
rejected or failed — produces:

* one JSON line appended to ``logs/runs.jsonl`` (the fields the monitoring report
  aggregates), and
* one file ``traces/<run_id>.json`` with the full ordered timeline: every
  guardrail decision, every tool call with its arguments and (redacted) output,
  every approval request and decision, the final output and any errors.

Both are redacted with :class:`~salesops_agent.redaction.Redactor` before they
touch disk. Their shapes are declared in ``schemas/run_log.schema.json`` and
``schemas/trace.schema.json``, and the tests validate real output against them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from salesops_agent.redaction import Redactor

LOG_FILE = "runs.jsonl"


def utc_now() -> str:
    """ISO-8601 UTC timestamp with seconds precision."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_log_entry(result: Any, rec: Any, *, question: str, timestamp: str, redactor: Redactor,
                    context: dict[str, Any]) -> dict[str, Any]:
    """Assemble the one-line run log record from a finished run."""
    interventions = [
        {k: v for k, v in g.items() if k in ("layer", "reason", "tool", "detail", "policy")}
        for g in rec.blocked_events
    ]
    return {
        "run_id": result.run_id,
        "timestamp": timestamp,
        "user_input": redactor.values(question),
        "task_id": context.get("task_id"),
        "user_id": context.get("user_id"),
        "session_id": context.get("session_id"),
        "thread_id": context.get("thread_id"),
        "final_status": result.status,
        "tools_called": [{"tool": t["tool"], "status": t["status"]} for t in rec.tool_events],
        "tool_call_count": len(rec.tool_events),
        "latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost_usd": result.estimated_cost_usd,
        "usage_is_estimate": result.usage_is_estimate,
        "guardrail_interventions": redactor.obj(interventions),
        "hitl_decision": result.hitl_decision,
        "hitl_approvals": [{"tool": a["tool"], "decision": a["decision"], "source": a["source"]} for a in rec.approvals],
        "failure_reason": result.failure_reason,
        "model": context.get("model"),
        "versions": result.versions,
        "trace_file": f"traces/{result.run_id}.json",
    }


def build_trace(result: Any, rec: Any, *, question: str, timestamp: str, redactor: Redactor,
                raw_answer: str | None, context: dict[str, Any]) -> dict[str, Any]:
    """Assemble the full trace document for one run."""
    events = [redactor.tool_event(ev) if ev["kind"] == "tool" else redactor.obj(ev) for ev in rec.events]
    return {
        "run_id": result.run_id,
        "timestamp": timestamp,
        "input": redactor.values(question),
        "context": {k: context.get(k) for k in ("task_id", "user_id", "session_id", "thread_id", "model")},
        "versions": result.versions,
        "status": result.status,
        "selected_tools": [t["tool"] for t in rec.tool_events],
        "tool_calls": [redactor.tool_event(t) for t in rec.tool_events],
        "guardrail_decisions": redactor.obj(rec.guardrail_events),
        "hitl": {
            "approval_requested": bool(rec.approvals),
            "decision": result.hitl_decision,
            "requests": redactor.obj(rec.approvals),
        },
        "model_output": redactor.text(raw_answer) if raw_answer else raw_answer,
        "final_output": redactor.text(result.answer) if result.answer else result.answer,
        "blocked": result.blocked,
        "errors": [redactor.text(e) for e in rec.errors],
        "failure_reason": redactor.text(result.failure_reason) if result.failure_reason else None,
        "latency_ms": result.latency_ms,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                  "estimated_cost_usd": result.estimated_cost_usd, "usage_is_estimate": result.usage_is_estimate},
        "timeline": events,
    }


def write_run_artifacts(logs_dir: Path, traces_dir: Path, entry: dict[str, Any], trace: dict[str, Any]) -> Path:
    """Append the log line and write the trace file; return the trace path."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    traces_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / LOG_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    path = traces_dir / f"{entry['run_id']}.json"
    path.write_text(json.dumps(trace, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def read_log(logs_dir: Path) -> list[dict[str, Any]]:
    """Read every run record; malformed lines are skipped and counted, never fatal."""
    path = logs_dir / LOG_FILE
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_malformed": True})
    return rows
