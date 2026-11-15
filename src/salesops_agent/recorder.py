"""Per-run event recorder.

One :class:`RunRecorder` is created for each question. The guardrails, the tool
middleware and the approval gate append to it as the run proceeds; at the end,
the service turns it into the run result (and, once logging is wired in, the log
entry and trace artifact). Passing the recorder explicitly, rather than using a
module-level list, keeps concurrent runs from mixing their events.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunRecorder:
    """Ordered record of what happened during one run."""

    run_id: str
    started: float = field(default_factory=time.perf_counter)
    events: list[dict[str, Any]] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    hitl: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)

    def event(self, kind: str, **data: Any) -> None:
        """Append a timestamped event to the run's timeline."""
        self.events.append({"t_ms": self._elapsed_ms(), "kind": kind, **data})

    def guardrail(self, decision: Any) -> None:
        """Record a guardrail decision (allowed or blocked)."""
        record = decision.to_dict()
        self.guardrail_events.append(record)
        self.event("guardrail", **record)

    def tool_event(self, name: str, args: dict[str, Any], status: str, output: Any) -> None:
        """Record a tool call outcome: ``executed``, ``blocked`` or ``rejected``."""
        record = {"tool": name, "args": args, "status": status, "output": output}
        self.tool_events.append(record)
        self.event("tool", **record)

    def error(self, message: str) -> None:
        """Record an error message."""
        self.errors.append(message)
        self.event("error", message=message)

    @property
    def blocked_events(self) -> list[dict[str, Any]]:
        return [g for g in self.guardrail_events if g.get("action") == "blocked"]
