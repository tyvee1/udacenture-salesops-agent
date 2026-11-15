"""Human-in-the-loop approval: single-use grants bound to the exact action approved.

How the gate cannot be bypassed
-------------------------------
Pausing the agent before ``send_email`` is only half of a gate; the other half
is making sure nothing *else* can send. So the outbox itself refuses to write
unless the current execution context holds an :class:`ApprovalGrant` whose hash
matches the exact recipient, subject and body being sent, and which has not
been used. The only code that creates a grant is the approval middleware, after
a human (or the ``--approval`` flag standing in for one) says ``approve``.

Consequences, each covered by a test:

* calling the ``send_email`` function directly, or ``EmailOutbox.deliver`` from
  any other tool, sends nothing — there is no grant;
* an approval for one email cannot send a different one — the hash differs;
* one approval cannot send twice — the grant is consumed;
* any tool marked ``risk: high`` in the registry is paused, so adding a new
  sending tool does not silently skip approval.

The grant travels in a :mod:`contextvars` variable, which is copied into the
worker thread LangChain uses to run a tool, and is cleared when the tool returns.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

VALID_DECISIONS = ("approve", "reject")

_current_grant: contextvars.ContextVar["ApprovalGrant | None"] = contextvars.ContextVar("approval_grant", default=None)


class ApprovalRequired(PermissionError):
    """Raised when a high-risk action is attempted without a valid, matching, unused grant."""


def action_hash(tool: str, args: dict[str, Any]) -> str:
    """Stable hash of a tool name and its arguments (key order does not matter)."""
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ApprovalGrant:
    """Permission to perform exactly one specific action, once."""

    tool: str
    digest: str
    source: str
    used: bool = field(default=False)


@contextmanager
def grant_scope(tool: str, args: dict[str, Any], source: str) -> Iterator[ApprovalGrant]:
    """Make a grant for this exact action available while the tool runs, then remove it."""
    grant = ApprovalGrant(tool=tool, digest=action_hash(tool, args), source=source)
    token = _current_grant.set(grant)
    try:
        yield grant
    finally:
        _current_grant.reset(token)


def consume(tool: str, args: dict[str, Any]) -> ApprovalGrant:
    """Use up the current grant for this action, or raise :class:`ApprovalRequired`."""
    grant = _current_grant.get()
    if grant is None:
        raise ApprovalRequired(f"{tool} requires human approval; none was given for this action")
    if grant.used:
        raise ApprovalRequired(f"the approval for {tool} has already been used")
    if grant.tool != tool or grant.digest != action_hash(tool, args):
        raise ApprovalRequired(f"the approval given does not match this {tool} action")
    grant.used = True
    return grant


def normalize_decision(value: Any) -> tuple[str, str]:
    """Turn a resume value into (decision, source). Anything unrecognized is a rejection."""
    if isinstance(value, dict):
        decision, source = str(value.get("decision", "")).lower(), str(value.get("source", "unknown"))
    else:
        decision, source = str(value).lower(), "unknown"
    if decision not in VALID_DECISIONS:
        return "reject", f"{source} (unrecognized decision {decision!r} treated as reject)"
    return decision, source
