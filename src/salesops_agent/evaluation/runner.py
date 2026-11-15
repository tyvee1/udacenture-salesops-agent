"""``uv run evaluate`` — run every task, check expectations, write the report.

Modes
-----
``--mode auto`` (default) uses the configured model when a key resolves and
falls back to each task's script when it does not, so the suite always runs and
the report says which mode produced each result. ``--mode live`` requires a key;
``--mode offline`` never calls a model. Tasks declared ``model: scripted`` use
their script in every mode, because they test a control against a deliberately
misbehaving model.

Exit code is 0 when every task passes and 1 otherwise, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from salesops_agent import service
from salesops_agent.config import ConfigError, Settings, load_settings, resolve_api_key
from salesops_agent.evaluation.report import write_reports
from salesops_agent.evaluation.tasks import EvalTask, load_tasks, script_to_model
from salesops_agent.tools.email import EmailOutbox


@dataclass
class TaskResult:
    """Outcome of one task, as written to the report."""

    task_id: str
    category: str
    mode: str
    success: bool
    latency_ms: float
    estimated_cost_usd: float
    usage_is_estimate: bool
    run_status: str
    run_id: str | None
    failure_reason: str | None = None
    checks: list[str] = field(default_factory=list)


def check_expectations(task: EvalTask, result: Any, outbox_delta: int) -> list[str]:
    """Compare a run against the task's expectations; return one message per failed check.

    ``result`` is a :class:`~salesops_agent.service.RunResult`. Fields that a
    given build does not produce yet read as ``None``/empty, so an expectation on
    them fails with a clear message rather than crashing the suite.
    """
    exp = task.expect
    failures: list[str] = []
    answer = result.answer or ""
    blocks = getattr(result, "guardrail_events", []) or []
    blocked = [e for e in blocks if e.get("action") == "blocked"]
    hitl = getattr(result, "hitl_decision", None)

    if "status" in exp and result.status != exp["status"]:
        failures.append(f"status {result.status!r} != expected {exp['status']!r}")
    if "blocked_layer" in exp and not any(e.get("layer") == exp["blocked_layer"] for e in blocked):
        failures.append(f"no '{exp['blocked_layer']}' guardrail block recorded")
    if "blocked_reason" in exp and not any(e.get("reason") == exp["blocked_reason"] for e in blocked):
        got = sorted({e.get("reason") for e in blocked}) or "none"
        failures.append(f"block reason {exp['blocked_reason']!r} not recorded (got {got})")
    for name in exp.get("tools_called_include", []):
        if name not in result.tools_called:
            failures.append(f"model never proposed {name}")
    for name in exp.get("tools_executed_include", []):
        if name not in result.tools_executed:
            failures.append(f"{name} did not execute")
    for name in exp.get("tools_not_executed", []):
        if name in result.tools_executed:
            failures.append(f"{name} executed but must not have")
    if "answer_contains_any" in exp and not any(s in answer for s in exp["answer_contains_any"]):
        failures.append(f"answer contains none of {exp['answer_contains_any']}")
    for s in exp.get("answer_not_contains", []):
        if s in answer:
            failures.append(f"answer contains forbidden text {s!r}")
    if "outbox_delta" in exp and outbox_delta != exp["outbox_delta"]:
        failures.append(f"outbox changed by {outbox_delta}, expected {exp['outbox_delta']}")
    if "hitl_decision" in exp and hitl != exp["hitl_decision"]:
        failures.append(f"approval decision {hitl!r} != expected {exp['hitl_decision']!r}")
    return failures


def _live_available(settings: Settings) -> bool:
    try:
        resolve_api_key(settings)
        return True
    except ConfigError:
        return False


def run_task(task: EvalTask, settings: Settings, mode: str) -> TaskResult:
    """Run one task in the given mode and check it. Exceptions become failed results, never crashes."""
    use_script = task.model == "scripted" or mode == "offline"
    task_mode = "scripted" if task.model == "scripted" else ("offline" if use_script else "live")
    model = script_to_model(task) if use_script else None
    outbox = EmailOutbox(settings.path("state_dir") / "email_outbox.jsonl")
    before = outbox.count()

    kwargs: dict[str, Any] = {"settings": settings, "model": model}
    if task.approval is not None:
        kwargs["approval"] = task.approval

    started = time.perf_counter()
    try:
        result = service.ask(task.input, **kwargs)
    except Exception as exc:  # a crash is a task failure with a reason, not a suite abort
        return TaskResult(
            task_id=task.id, category=task.category, mode=task_mode, success=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 1), estimated_cost_usd=0.0,
            usage_is_estimate=False, run_status="error", run_id=None,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
    failures = check_expectations(task, result, outbox.count() - before)
    return TaskResult(
        task_id=task.id, category=task.category, mode=task_mode, success=not failures,
        latency_ms=result.latency_ms, estimated_cost_usd=result.estimated_cost_usd,
        usage_is_estimate=result.usage_is_estimate, run_status=result.status, run_id=result.run_id,
        failure_reason="; ".join(failures) or None, checks=sorted(task.expect),
    )


def run_suite(settings: Settings, mode: str = "auto", only: list[str] | None = None) -> tuple[list[TaskResult], str]:
    """Run all (or selected) tasks; return results and the resolved mode."""
    resolved = mode
    if mode == "auto":
        resolved = "live" if _live_available(settings) else "offline"
    elif mode == "live" and not _live_available(settings):
        resolve_api_key(settings)  # raises with the actionable message
    tasks = load_tasks(settings.root / "evals" / "tasks.yaml")
    if only:
        tasks = [t for t in tasks if t.id in set(only)]
    return [run_task(t, settings, resolved) for t in tasks], resolved


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``uv run evaluate``."""
    parser = argparse.ArgumentParser(prog="evaluate", description="Run the SalesOps evaluation suite")
    parser.add_argument("--mode", choices=["auto", "live", "offline"], default="auto")
    parser.add_argument("--task", action="append", dest="only", help="Run only this task id (repeatable)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        results, mode = run_suite(settings, args.mode, args.only)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"Evaluation mode: {mode}" + ("" if mode == "live" else "  (no model key found: live tasks use their scripts)"))
    for r in results:
        mark = "PASS" if r.success else "FAIL"
        print(f"  {mark}  {r.task_id:<42} {r.latency_ms:>8.1f} ms  ${r.estimated_cost_usd:.6f}  [{r.mode}]"
              + (f"  — {r.failure_reason}" if r.failure_reason else ""))
    paths = write_reports([asdict(r) for r in results], mode, settings)
    passed = sum(r.success for r in results)
    print(f"\n{passed}/{len(results)} tasks passed. Reports: " + ", ".join(str(p.relative_to(settings.root)) for p in paths))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
