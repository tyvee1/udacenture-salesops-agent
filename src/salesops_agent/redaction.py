"""Redaction for logs and traces.

Logs and traces exist so someone can debug a run later; they must not become a
second copy of the restricted data. Four rules, applied in this order:

1. **Restricted tools.** The output of any tool whose ``data_access`` the policy
   denies is replaced wholesale. (Such tools are blocked before they run, so
   this is a backstop rather than the main control.)
2. **Restricted keys.** Any dictionary key that is a restricted field name
   (``salary_usd``, ``confidential_strategy``…) has its value replaced.
3. **Leak-shaped text.** Model output, tool-output strings and guardrail match
   excerpts are scanned with the output guardrail's detector, one string leaf at
   a time. A string that would have been blocked is replaced with
   ``[REDACTED: <reason>]``. Scanning leaves individually keeps ordinary CRM
   fields readable while removing the sentence that leaks.
4. **User input** is kept verbatim so a reviewer can see what was asked, except
   that restricted values (strategy keys, raw field names, restricted figures
   written as money) are masked.

Rule 4 over-redacts slightly: a question mentioning ``$180,000`` (a real
opportunity amount that is also an HR salary) is masked in the log. Redacting
too much in a log is the safe direction to be wrong in.
"""

from __future__ import annotations

import re
from typing import Any

from salesops_agent.guardrails.checks import scan_output
from salesops_agent.guardrails.policy import Policy
from salesops_agent.guardrails.sensitive import SensitiveIndex

REDACTED = "[REDACTED]"


class Redactor:
    """Applies the four redaction rules with one policy and sensitive index."""

    def __init__(self, policy: Policy, index: SensitiveIndex, denied_tools: set[str] | None = None):
        self.policy = policy
        self.index = index
        self.denied_tools = denied_tools or set()
        markers = set(policy.restricted_field_markers) | set(index.phrases)
        self._keys = {m.lower() for m in markers}
        values = sorted(markers | {n for n in index.numbers if any(c in n for c in "$,mk")}, key=len, reverse=True)
        self._values = re.compile("|".join(re.escape(v) for v in values), re.IGNORECASE) if values else None

    def text(self, value: str) -> str:
        """Rule 3: replace a leak-shaped string; otherwise mask restricted values inside it."""
        hit = scan_output(value, self.policy, self.index)
        if hit:
            return f"[REDACTED: {hit[0]}]"
        return self.values(value)

    def values(self, value: str) -> str:
        """Rule 4: mask restricted values but keep the rest of the text."""
        return self._values.sub(REDACTED, value) if self._values else value

    def obj(self, value: Any) -> Any:
        """Rules 2 and 3 applied recursively to dicts, lists and strings."""
        if isinstance(value, dict):
            return {k: (REDACTED if str(k).lower() in self._keys else self.obj(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def tool_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Rule 1, then rules 2-3, for one recorded tool call."""
        out = dict(event)
        if out.get("tool") in self.denied_tools and out.get("status") == "executed":
            out["output"] = "[REDACTED: restricted tool output]"
        else:
            out["output"] = self.obj(out.get("output"))
        out["args"] = self.obj(out.get("args"))
        return out
