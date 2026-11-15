"""Load evaluation tasks from ``evals/tasks.yaml`` and turn their scripts into model turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from salesops_agent.scripted_model import ScriptedChatModel, answer, tool_call

#: Every expectation key the checker understands. An unknown key is a typo, not a no-op.
KNOWN_EXPECTATIONS = frozenset({
    "status", "blocked_layer", "blocked_reason", "tools_called_include", "tools_executed_include",
    "tools_not_executed", "answer_contains_any", "answer_not_contains", "outbox_delta", "hitl_decision",
})


@dataclass(frozen=True)
class EvalTask:
    """One evaluation task as declared in YAML."""

    id: str
    category: str
    description: str
    input: str
    model: str  # "live" or "scripted"
    script: list[dict[str, Any]]
    expect: dict[str, Any]
    approval: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def load_tasks(path: Path) -> list[EvalTask]:
    """Parse the task file, rejecting duplicate IDs and unknown expectation keys."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for entry in raw.get("tasks", []):
        task = EvalTask(
            id=entry["id"],
            category=entry["category"],
            description=" ".join(str(entry.get("description", "")).split()),
            input=entry["input"],
            model=entry.get("model", "live"),
            script=entry.get("script") or [],
            expect=entry.get("expect") or {},
            approval=entry.get("approval"),
        )
        if task.id in seen:
            raise ValueError(f"{path}: duplicate task id {task.id}")
        unknown = set(task.expect) - KNOWN_EXPECTATIONS
        if unknown:
            raise ValueError(f"{path}: task {task.id} has unknown expectation keys {sorted(unknown)}")
        if task.model not in {"live", "scripted"}:
            raise ValueError(f"{path}: task {task.id} model must be 'live' or 'scripted'")
        seen.add(task.id)
        tasks.append(task)
    return tasks


def script_to_model(task: EvalTask) -> ScriptedChatModel:
    """Convert a task's script into a :class:`ScriptedChatModel`."""
    turns: list[Any] = []
    for i, step in enumerate(task.script):
        if "tool" in step:
            turns.append(tool_call(step["tool"], step.get("args") or {}, call_id=f"{task.id}-{i}"))
        elif "answer" in step:
            turns.append(answer(step["answer"]))
        elif "answer_quoting_last_tool" in step:
            prefix = step["answer_quoting_last_tool"]

            def quote(messages, _prefix=prefix):
                last_tool = next((m for m in reversed(messages) if m.type == "tool"), None)
                body = last_tool.content if last_tool is not None else "(no tool result)"
                return answer(f"{_prefix} {body}")

            turns.append(quote)
        else:
            raise ValueError(f"task {task.id}: unrecognized script step {step}")
    return ScriptedChatModel(turns=turns)
