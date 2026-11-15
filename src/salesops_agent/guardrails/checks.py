"""The guardrail decisions: input, output, email content and tool calls.

All four are plain functions over text and policy, with no model involved, so
each can be tested exhaustively and none can be talked out of its decision.
:mod:`salesops_agent.guardrails.middleware` wires the tool check into the agent;
:mod:`salesops_agent.service` runs the input check before the agent exists and
the output check after it finishes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from salesops_agent.guardrails.policy import Policy, normalize
from salesops_agent.guardrails.sensitive import EXEC_TITLES, MONEY, SensitiveIndex


@dataclass
class Decision:
    """One guardrail decision. ``action`` is ``allowed`` or ``blocked``."""

    layer: str  # input | tool | output
    action: str
    policy: str
    reason: str | None = None
    tool: str | None = None
    detail: str | None = None
    matched: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == "blocked"

    def to_dict(self) -> dict[str, Any]:
        """Dict form for logs and traces; empty fields dropped."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    def blocked_payload(self) -> dict[str, Any]:
        """The explicit status object returned to the caller for a blocked action."""
        payload = {"status": "blocked", "reason": self.reason, "policy": self.policy}
        if self.tool:
            payload["tool"] = self.tool
        if self.detail:
            payload["detail"] = self.detail
        return payload


def _excerpt(text: str, start: int, end: int, pad: int = 20) -> str:
    """A short window around a match, for the decision record (never the whole input)."""
    return text[max(0, start - pad): end + pad]


# --------------------------------------------------------------------------- input


def classify_text(text: str, policy: Policy, skip: set[str] | None = None) -> tuple[str, str] | None:
    """Return (reason, matched excerpt) for the first input category ``text`` falls in, or None."""
    norm = normalize(text)
    for cat in policy.input_categories:
        if skip and cat.reason in skip:
            continue
        candidate = norm
        for ex in cat.exempt:
            candidate = ex.sub(" ", candidate)
        for pat in cat.patterns:
            m = pat.search(candidate)
            if m:
                return cat.reason, _excerpt(candidate, m.start(), m.end())
    return None


def check_input(text: str, policy: Policy) -> Decision:
    """Screen a user request before the agent sees it."""
    hit = classify_text(text, policy)
    if hit:
        return Decision("input", "blocked", policy.name, reason=hit[0], matched=hit[1])
    return Decision("input", "allowed", policy.name)


# -------------------------------------------------------------------------- output


def _near(spans_a: list[tuple[int, int]], spans_b: list[tuple[int, int]], window: int) -> bool:
    return any(abs(a0 - b0) <= window or abs(a1 - b1) <= window for a0, a1 in spans_a for b0, b1 in spans_b)


def _anchors(text: str, kind: str, index: SensitiveIndex) -> list[tuple[int, int]]:
    spans = index.people_in(text)
    if kind == "people_or_money":
        spans += [(m.start(), m.end()) for m in EXEC_TITLES.finditer(text)]
        spans += [(m.start(), m.end()) for m in MONEY.finditer(text)]
        spans += index.numbers_in(text)
    return spans


def scan_output(text: str, policy: Policy, index: SensitiveIndex) -> tuple[str, str] | None:
    """Return (reason, excerpt) if ``text`` contains restricted content, else None.

    Order: raw field names and strategy markers first (unambiguous), then each
    category in policy order. Categories with an anchor only block when a
    sensitive term sits near an employee name, executive title or money amount,
    which is what lets "Acme Corp is owned by Maya Chen" through while stopping
    "Marcus Thorne has a $2M equity bonus".
    """
    norm = normalize(text)
    for marker in policy.restricted_field_markers:
        pos = norm.find(marker)
        if pos >= 0:
            return policy.raw_field_reason, _excerpt(norm, pos, pos + len(marker))
    for phrase in index.phrases:
        pos = norm.find(phrase)
        if pos >= 0:
            return "confidential_strategy", _excerpt(norm, pos, pos + len(phrase))

    for cat in policy.output_categories:
        candidate = norm
        for ex in cat.exempt:
            candidate = ex.sub(" ", candidate)
        if cat.strong:
            m = cat.strong.search(candidate)
            if m:
                return cat.reason, _excerpt(candidate, m.start(), m.end())
        terms = [(m.start(), m.end()) for m in cat.terms.finditer(candidate)]
        if not terms:
            continue
        if cat.anchor is None or _near(terms, _anchors(candidate, cat.anchor, index), policy.output_proximity):
            s, e = terms[0]
            return cat.reason, _excerpt(candidate, s, e)
    # Restricted figures are deliberately NOT blocked on their own: 180000 is both a
    # salary in the HR file and a real opportunity amount in the CRM. They act only as
    # anchors for compensation terms (see _anchors).
    return None


def check_output(text: str, policy: Policy, index: SensitiveIndex) -> Decision:
    """Screen the agent's final answer before it reaches the user."""
    hit = scan_output(text, policy, index)
    if hit:
        return Decision("output", "blocked", policy.name, reason=hit[0], matched=hit[1])
    return Decision("output", "allowed", policy.name)


# --------------------------------------------------------------------------- email


def check_email_content(subject: str, body: str, policy: Policy, index: SensitiveIndex) -> Decision:
    """Screen a customer email: restricted content, invented discounts, legal commitments, deal secrets."""
    text = f"{subject}\n{body}"
    hit = scan_output(text, policy, index)
    if hit:
        return Decision("tool", "blocked", policy.name, reason=policy.email_reason, tool="send_email",
                        detail=f"restricted content: {hit[0]}", matched=hit[1])
    norm = normalize(text)
    for claim in policy.email_claims:
        m = claim.pattern.search(norm)
        if m:
            return Decision("tool", "blocked", policy.name, reason=policy.email_reason, tool="send_email",
                            detail=claim.name, matched=_excerpt(norm, m.start(), m.end()))
    return Decision("tool", "allowed", policy.name, tool="send_email")


# ---------------------------------------------------------------------------- tool


@dataclass
class ToolCheckContext:
    """What the tool guardrail needs to know beyond the call itself."""

    policy: Policy
    index: SensitiveIndex
    specs: dict[str, Any]
    known_recipients: set[str] = field(default_factory=set)


def _string_args(args: dict[str, Any]) -> list[str]:
    return [v for v in args.values() if isinstance(v, str)]


def check_tool_call(name: str, args: dict[str, Any], ctx: ToolCheckContext) -> Decision:
    """Decide whether a proposed tool call may run. Runs before the tool body, every time.

    Checks, in order:

    1. the tool is in the registry;
    2. its declared data access is not denied (restricted HR / financial);
    3. no argument names a restricted field;
    4. no string argument asks for a restricted topic (input categories reused);
    5. for ``send_email``: the recipient is a CRM contact and the content is safe.

    Approval for high-risk tools is a separate step applied after these checks
    pass, so a human is never asked to approve something policy already forbids.
    """
    policy = ctx.policy
    spec = ctx.specs.get(name)
    if spec is None:
        return Decision("tool", "blocked", policy.name, reason="unregistered_tool", tool=name,
                        detail="tool is not in config/tools.yaml")
    denied = policy.denied_data_access.get(spec.data_access)
    if denied:
        return Decision("tool", "blocked", policy.name, reason=denied, tool=name,
                        detail=f"data_access={spec.data_access} is not permitted for SalesOps requests")

    flat = normalize(json.dumps(args, default=str))
    for marker in policy.restricted_field_markers:
        if marker in flat:
            return Decision("tool", "blocked", policy.name, reason="restricted_field_request", tool=name,
                            detail=f"argument names restricted field '{marker}'")

    if policy.screen_tool_arguments and name != "send_email":
        for value in _string_args(args):
            hit = classify_text(value, policy, skip={"prompt_injection"})
            if hit:
                return Decision("tool", "blocked", policy.name, reason="out_of_scope_query", tool=name,
                                detail=f"argument requests restricted topic: {hit[0]}", matched=hit[1])

    if name == "send_email":
        to = str(args.get("to", "")).strip().lower()
        if to not in ctx.known_recipients:
            return Decision("tool", "blocked", policy.name, reason="unknown_recipient", tool=name,
                            detail="recipient is not a contact in the CRM")
        return check_email_content(str(args.get("subject", "")), str(args.get("body", "")), policy, ctx.index)

    return Decision("tool", "allowed", policy.name, tool=name)


def blocked_tool_content(decision: Decision) -> str:
    """JSON text the model receives in place of a blocked tool's result."""
    return json.dumps(decision.blocked_payload())
