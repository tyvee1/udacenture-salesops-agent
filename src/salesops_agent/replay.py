"""Replayable run history: inspect a past run, see what has changed since, and re-run it.

This answers the scenario's original question — "the same prompt behaved
differently a week later; was it the prompt, a tool, a dependency or the
config?" — in two steps:

1. ``salesops-agent replay <run_id>`` reads the recorded run (no model, no key)
   and compares its version fingerprint with the current files. Each differing
   hash names a component that changed between then and now.
2. ``salesops-agent replay <run_id> --rerun`` asks the same question again under
   the same user id (in a fresh thread, so the re-run neither reads nor adds to
   the original conversation's memory) and prints the two runs side by side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Fingerprint fields compared between runs, with the component each one names.
COMPONENTS = {
    "prompt_sha256": "prompt text",
    "prompt_version": "prompt version",
    "agent_config_sha256": "agent configuration (config/agent.yaml)",
    "tool_registry_sha256": "tool registry (config/tools.yaml)",
    "policy_sha256": "guardrail policy (config/policy.yaml)",
    "uv_lock_sha256": "dependencies (uv.lock)",
    "model": "model",
    "git_commit": "code (git commit)",
}


def version_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable list of components whose fingerprint differs between two runs."""
    return [label for key, label in COMPONENTS.items() if before.get(key) != after.get(key)]


def load_trace(root: Path, trace_file: str) -> dict[str, Any] | None:
    path = root / trace_file
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def outcome(trace: dict[str, Any]) -> dict[str, Any]:
    """The comparable outcome of a run, from its trace."""
    return {
        "status": trace.get("status"),
        "tools": [f"{t['tool']}:{t['status']}" for t in trace.get("tool_calls", [])],
        "guardrail_blocks": sorted(f"{g['layer']}:{g.get('reason')}" for g in trace.get("guardrail_decisions", [])
                                   if g.get("action") == "blocked"),
        "hitl_decision": (trace.get("hitl") or {}).get("decision"),
    }


def compare_outcomes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Lines describing how two run outcomes differ; empty when they match."""
    return [f"{k}: {old.get(k)!r} -> {new.get(k)!r}" for k in ("status", "tools", "guardrail_blocks", "hitl_decision")
            if old.get(k) != new.get(k)]
