"""``uv run generate-monitoring-report`` — aggregate ``logs/runs.jsonl`` into an operational report.

The report is aggregated, not a log dump: counts, rates, averages and
percentiles, broken down the ways an operator needs them. Every sentence in the
"How to read this report" section that states a fact is generated from the same
numbers as the tables, so the prose cannot disagree with the data.

Definitions used throughout (also printed in the report):

* **Success** — the run ended as designed: ``completed``, ``blocked`` (a
  guardrail stopped it) or ``rejected`` (a human declined the action). A
  guardrail block is the system working, not failing.
* **Failure** — the run ended in ``error``: a model, provider, configuration or
  runtime failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from salesops_agent.config import ConfigError, load_settings
from salesops_agent.observability import read_log

SUCCESS_STATUSES = frozenset({"completed", "blocked", "rejected"})


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(q * len(ordered) + 0.5)) - 1))
    return ordered[k]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute every metric in the report from run-log rows."""
    malformed = sum(1 for r in rows if r.get("_malformed"))
    rows = [r for r in rows if not r.get("_malformed")]
    n = len(rows)
    statuses = Counter(r.get("final_status", "unknown") for r in rows)
    successes = sum(statuses[s] for s in SUCCESS_STATUSES)
    failures = statuses.get("error", 0)
    latencies = [float(r.get("latency_ms") or 0) for r in rows]
    costs = [float(r.get("estimated_cost_usd") or 0) for r in rows]

    tool_calls: Counter[str] = Counter()
    tool_outcomes: Counter[str] = Counter()
    for r in rows:
        for t in r.get("tools_called", []):
            tool_calls[t["tool"]] += 1
            tool_outcomes[f"{t['tool']}:{t['status']}"] += 1

    blocks = [g for r in rows for g in r.get("guardrail_interventions", [])]
    by_layer = Counter(g.get("layer", "unknown") for g in blocks)
    by_reason = Counter(f"{g.get('layer', '?')}:{g.get('reason', '?')}" for g in blocks)
    runs_with_block = sum(1 for r in rows if r.get("guardrail_interventions"))

    approvals = [a for r in rows for a in r.get("hitl_approvals", [])]
    decisions = Counter(a.get("decision") for a in approvals)
    sources = Counter(a.get("source") for a in approvals)

    failure_reasons = Counter((r.get("failure_reason") or "").split(":")[0] or "unspecified"
                              for r in rows if r.get("final_status") == "error")

    prompt_versions = Counter(str((r.get("versions") or {}).get("prompt_version")) for r in rows)
    models = Counter(r.get("model") or "unknown" for r in rows)
    eval_runs = [r for r in rows if r.get("task_id")]
    timestamps = sorted(r["timestamp"] for r in rows if r.get("timestamp"))

    return {
        "total_runs": n,
        "malformed_lines_skipped": malformed,
        "window": {"first": timestamps[0] if timestamps else None, "last": timestamps[-1] if timestamps else None},
        "success_count": successes,
        "failure_count": failures,
        "success_rate": round(successes / n, 4) if n else 0.0,
        "failure_rate": round(failures / n, 4) if n else 0.0,
        "status_counts": dict(sorted(statuses.items())),
        "latency_ms": {
            "average": round(sum(latencies) / n, 1) if n else 0.0,
            "median": round(median(latencies), 1) if n else 0.0,
            "p95": round(_pct(latencies, 0.95), 1),
            "max": round(max(latencies), 1) if n else 0.0,
        },
        "estimated_cost_usd": {
            "total": round(sum(costs), 8),
            "average_per_run": round(sum(costs) / n, 8) if n else 0.0,
            "runs_with_estimated_usage": sum(1 for r in rows if r.get("usage_is_estimate")),
        },
        "tool_call_counts": dict(tool_calls.most_common()),
        "tool_call_outcomes": dict(sorted(tool_outcomes.items())),
        "total_tool_calls": sum(tool_calls.values()),
        "guardrail_blocks": {
            "total": len(blocks),
            "runs_with_a_block": runs_with_block,
            "by_layer": dict(sorted(by_layer.items())),
            "by_reason": dict(by_reason.most_common()),
        },
        "hitl": {
            "approval_requests": len(approvals),
            "approvals": decisions.get("approve", 0),
            "rejections": decisions.get("reject", 0),
            "by_source": dict(sources.most_common()),
        },
        "common_failure_reasons": dict(failure_reasons.most_common(10)),
        "evaluation_runs": len(eval_runs),
        "prompt_versions_seen": dict(sorted(prompt_versions.items())),
        "models_seen": dict(models.most_common()),
    }


def _table(header: tuple[str, str], items: dict[str, Any]) -> list[str]:
    lines = [f"| {header[0]} | {header[1]} |", "| --- | ---: |"]
    lines += [f"| {k} | {v} |" for k, v in items.items()] or ["| (none) | 0 |"]
    return lines


def interpretation(m: dict[str, Any]) -> list[str]:
    """Generated reading notes. Each factual clause is computed from ``m``; none are hard-coded."""
    if not m["total_runs"]:
        return ["No runs have been logged yet. Run the agent or `uv run evaluate`, then regenerate this report."]
    notes = [
        f"- **Reliability.** {m['success_count']} of {m['total_runs']} runs ended as designed "
        f"({m['success_rate']:.1%}); {m['failure_count']} ended in an error ({m['failure_rate']:.1%}). "
        + ("Investigate the failure reasons below first." if m["failure_count"] else
           "No run failed, so the failure-reason table is empty by design."),
    ]
    gb = m["guardrail_blocks"]
    if gb["total"]:
        top_layer = max(gb["by_layer"], key=gb["by_layer"].get)
        notes.append(
            f"- **Guardrails.** {gb['total']} blocks across {gb['runs_with_a_block']} runs; the {top_layer} layer "
            f"blocked most ({gb['by_layer'][top_layer]}). A rising block rate on ordinary traffic suggests a false "
            f"positive; a block on the tool or output layer means an earlier layer let something through."
        )
    else:
        notes.append("- **Guardrails.** No blocks were recorded in this window.")
    h = m["hitl"]
    if h["approval_requests"]:
        default = h["by_source"].get("default_no_signal", 0)
        notes.append(
            f"- **Human approval.** {h['approval_requests']} approval requests: {h['approvals']} approved, "
            f"{h['rejections']} rejected"
            + (f", of which {default} were rejected automatically because no approval signal was given." if default
               else ".")
        )
    lat = m["latency_ms"]
    notes.append(
        f"- **Latency.** Median {lat['median']} ms, p95 {lat['p95']} ms. Runs stopped by the input guardrail never "
        f"call the model, so they pull the average down; compare p95 between reports rather than the mean."
    )
    c = m["estimated_cost_usd"]
    notes.append(
        f"- **Cost.** Estimated ${c['total']:.6f} in total, ${c['average_per_run']:.6f} per run. "
        + (f"{c['runs_with_estimated_usage']} runs used a scripted model whose token counts are estimated from text "
           f"length, so treat those costs as indicative." if c["runs_with_estimated_usage"] else
           "All usage figures came from the model provider.")
    )
    if len(m["prompt_versions_seen"]) > 1:
        notes.append(
            f"- **Versions.** Runs in this window used {len(m['prompt_versions_seen'])} prompt versions "
            f"({', '.join(m['prompt_versions_seen'])}). Compare runs by version before attributing a change in "
            f"behaviour to anything else."
        )
    return notes


def render_markdown(m: dict[str, Any], generated: str) -> str:
    """Render the metrics as a Markdown report."""
    lat, cost, gb, h = m["latency_ms"], m["estimated_cost_usd"], m["guardrail_blocks"], m["hitl"]
    lines = [
        "# Monitoring Report",
        "",
        f"Generated {generated} by `uv run generate-monitoring-report` from `logs/runs.jsonl`.",
        f"Window: {m['window']['first']} to {m['window']['last']}.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Total runs | {m['total_runs']} |",
        f"| Success rate | {m['success_rate']:.1%} |",
        f"| Failure rate | {m['failure_rate']:.1%} |",
        f"| Average latency (ms) | {lat['average']} |",
        f"| Median / p95 / max latency (ms) | {lat['median']} / {lat['p95']} / {lat['max']} |",
        f"| Estimated total cost (USD) | {cost['total']:.6f} |",
        f"| Estimated average cost per run (USD) | {cost['average_per_run']:.6f} |",
        f"| Tool calls | {m['total_tool_calls']} |",
        f"| Guardrail blocks | {gb['total']} |",
        f"| HITL approvals | {h['approvals']} |",
        f"| HITL rejections | {h['rejections']} |",
        f"| Evaluation-suite runs included | {m['evaluation_runs']} |",
        "",
        "Success means the run ended as designed (completed, blocked by a guardrail, or rejected by a human). "
        "Failure means it ended in an error.",
        "",
        "## Runs by final status",
        "",
        *_table(("Status", "Runs"), m["status_counts"]),
        "",
        "## Tool call counts",
        "",
        *_table(("Tool", "Calls"), m["tool_call_counts"]),
        "",
        "### Tool call outcomes",
        "",
        *_table(("Tool:outcome", "Calls"), m["tool_call_outcomes"]),
        "",
        "## Guardrail blocks",
        "",
        *_table(("Layer", "Blocks"), gb["by_layer"]),
        "",
        *_table(("Layer:reason", "Blocks"), gb["by_reason"]),
        "",
        "## Human-in-the-loop decisions",
        "",
        *_table(("Decision source", "Requests"), h["by_source"]),
        "",
        "## Common failure reasons",
        "",
        *_table(("Reason", "Runs"), m["common_failure_reasons"]),
        "",
        "## Versions and models",
        "",
        *_table(("Prompt version", "Runs"), m["prompt_versions_seen"]),
        "",
        *_table(("Model", "Runs"), m["models_seen"]),
        "",
        "## How to read this report",
        "",
        *interpretation(m),
        "",
    ]
    if m["malformed_lines_skipped"]:
        lines.insert(4, f"{m['malformed_lines_skipped']} malformed log lines were skipped.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``uv run generate-monitoring-report``."""
    parser = argparse.ArgumentParser(prog="generate-monitoring-report",
                                     description="Aggregate logs/runs.jsonl into reports/monitoring_report.{md,json}")
    parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    metrics = aggregate(read_log(settings.path("logs_dir")))
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = settings.path("reports_dir")
    out.mkdir(parents=True, exist_ok=True)
    (out / "monitoring_report.md").write_text(render_markdown(metrics, generated), encoding="utf-8")
    (out / "monitoring_report.json").write_text(json.dumps({"generated": generated, **metrics}, indent=2) + "\n",
                                                encoding="utf-8")
    print(f"{metrics['total_runs']} runs aggregated -> reports/monitoring_report.md, reports/monitoring_report.json")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
