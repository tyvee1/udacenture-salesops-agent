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
from typing import Any, Callable


@dataclass
class RunRecorder:
    """Ordered record of what happened during one run."""

    run_id: str
    started: float = field(default_factory=time.perf_counter)
    events: list[dict[str, Any]] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _seen: set[tuple[str, ...]] = field(default_factory=set, repr=False)
    #: Called as ``listener(kind, data)`` for every event — how optional external tracing
    #: sees guardrail and approval decisions without the controls knowing it exists.
    listeners: list[Callable[[str, dict[str, Any]], None]] = field(default_factory=list, repr=False)

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)

    def event(self, kind: str, **data: Any) -> None:
        """Append a timestamped event to the run's timeline."""
        self.events.append({"t_ms": self._elapsed_ms(), "kind": kind, **data})
        for listener in self.listeners:
            try:
                listener(kind, data)
            except Exception:  # an observer must never change the outcome of the run it observes
                pass

    def _first_time(self, *key: str) -> bool:
        """True the first time ``key`` is seen in this run.

        Resuming after an approval pause re-runs the paused tool step from its
        start, which would otherwise record its policy check and approval request
        twice; keying on the tool call id keeps one record per call.
        """
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def guardrail(self, decision: Any, call_id: str | None = None) -> None:
        """Record a guardrail decision (allowed or blocked), once per tool call."""
        if call_id and not self._first_time("guardrail", call_id):
            return
        record = decision.to_dict()
        self.guardrail_events.append(record)
        self.event("guardrail", **record)

    def tool_event(self, name: str, args: dict[str, Any], status: str, output: Any) -> None:
        """Record a tool call outcome: ``executed``, ``blocked`` or ``rejected``."""
        record = {"tool": name, "args": args, "status": status, "output": output}
        self.tool_events.append(record)
        self.event("tool", **record)

    def approval_requested(self, call_id: str, tool: str, args: dict[str, Any]) -> None:
        """Record that a high-risk action paused for human approval."""
        if not self._first_time("approval_requested", call_id):
            return
        entry = {"call_id": call_id, "tool": tool, "args": args, "requested": True, "decision": None, "source": None}
        self.approvals.append(entry)
        self.event("approval_requested", call_id=call_id, tool=tool, args=args)

    def approval_decided(self, call_id: str, decision: str, source: str) -> None:
        """Record the human decision for a paused action."""
        if not self._first_time("approval_decided", call_id):
            return
        for entry in self.approvals:
            if entry["call_id"] == call_id:
                entry.update(decision=decision, source=source)
        self.event("approval_decided", call_id=call_id, decision=decision, source=source)

    @property
    def hitl_decision(self) -> str | None:
        """Summary decision for the run: reject if any action was rejected, else approve, else None."""
        decisions = [a["decision"] for a in self.approvals if a["decision"]]
        if not decisions:
            return None
        return "reject" if "reject" in decisions else "approve"

    def error(self, message: str) -> None:
        """Record an error message."""
        self.errors.append(message)
        self.event("error", message=message)

    @property
    def blocked_events(self) -> list[dict[str, Any]]:
        return [g for g in self.guardrail_events if g.get("action") == "blocked"]
