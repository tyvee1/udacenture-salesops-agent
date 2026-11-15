"""Write the evaluation report as Markdown and JSON.

Every number in the Markdown summary is computed from the same result rows that
the JSON file holds, so the two can never disagree, and the summary sentences
are generated from those numbers rather than written by hand.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from salesops_agent.config import Settings


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-task rows into the headline figures."""
    n = len(rows)
    passed = sum(r["success"] for r in rows)
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r["success"])
    return {
        "tasks": n,
        "passed": passed,
        "failed": n - passed,
        "pass_rate": round(passed / n, 4) if n else 0.0,
        "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n, 1) if n else 0.0,
        "total_estimated_cost_usd": round(sum(r["estimated_cost_usd"] for r in rows), 8),
        "any_usage_estimated": any(r["usage_is_estimate"] for r in rows),
        "modes": dict(Counter(r["mode"] for r in rows)),
        "categories": {c: {"tasks": len(v), "passed": sum(v)} for c, v in sorted(by_cat.items())},
    }


def _md(rows: list[dict[str, Any]], summary: dict[str, Any], mode: str, generated: str) -> str:
    lines = [
        "# Evaluation Report",
        "",
        f"Generated {generated} by `uv run evaluate` (suite mode: **{mode}**).",
        "",
        f"**{summary['passed']} of {summary['tasks']} tasks passed** "
        f"({summary['pass_rate']:.0%}); average latency {summary['avg_latency_ms']} ms; "
        f"total estimated cost ${summary['total_estimated_cost_usd']:.6f}.",
        "",
        "Task modes: " + ", ".join(f"{k} = {v}" for k, v in sorted(summary["modes"].items())) + ". "
        "`live` used the configured model; `offline` used the task's script because no key was available; "
        "`scripted` tasks always use their script because they test a control against a deliberately "
        "misbehaving model.",
        "",
    ]
    if summary["any_usage_estimated"]:
        lines += [
            "Cost note: scripted and offline turns have no real tokenizer, so their token counts are "
            "estimated from text length (about four characters per token) and priced at the configured "
            "model's list rate.",
            "",
        ]
    lines += ["## Coverage by category", "", "| Category | Tasks | Passed |", "| --- | ---: | ---: |"]
    for cat, c in summary["categories"].items():
        lines.append(f"| {cat} | {c['tasks']} | {c['passed']} |")
    lines += [
        "",
        "## Per-task results",
        "",
        "| Task | Category | Mode | Result | Latency (ms) | Est. cost (USD) | Run status | Failure reason |",
        "| --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for r in rows:
        reason = (r["failure_reason"] or "").replace("|", "\\|")
        lines.append(
            f"| {r['task_id']} | {r['category']} | {r['mode']} | {'PASS' if r['success'] else 'FAIL'} | "
            f"{r['latency_ms']:.1f} | {r['estimated_cost_usd']:.6f} | {r['run_status']} | {reason} |"
        )
    return "\n".join(lines) + "\n"


def write_reports(rows: list[dict[str, Any]], mode: str, settings: Settings) -> list[Path]:
    """Write ``reports/evaluation_report.md`` and ``.json``; return their paths."""
    out_dir = settings.path("reports_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    summary = summarize(rows)
    md_path = out_dir / "evaluation_report.md"
    json_path = out_dir / "evaluation_report.json"
    md_path.write_text(_md(rows, summary, mode, generated), encoding="utf-8")
    json_path.write_text(
        json.dumps({"generated": generated, "mode": mode, "summary": summary, "tasks": rows}, indent=2) + "\n",
        encoding="utf-8",
    )
    return [md_path, json_path]
