"""Sensitive values derived from the restricted datasets themselves.

Rather than typing "Marcus Thorne" or "$2M" into a blocklist, the output
guardrail and the trace redactor build their list from the HR and financial
files at startup: every employee name, every numeric value, every strategy key.
If the HR file gains a record, the guardrail covers it without a code change.

Numbers are rendered in the forms a model is likely to write them —
``2000000``, ``2,000,000``, ``$2M``, ``2 million``, ``$2.0m``, ``850k`` — so a
reformatted figure is still caught.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from salesops_agent.data import RestrictedData

#: Money-looking amounts: "$2M", "$850,000", "2 million dollars", "850k".
MONEY = re.compile(
    r"(\$\s?\d[\d,]*(\.\d+)?\s?(k|m|mm|b|bn|million|billion|thousand)?\b)"
    r"|(\b\d[\d,]*(\.\d+)?\s?(k|m|mm|million|billion|thousand)\b)"
    r"|(\b\d[\d,]*(\.\d+)?\s?(usd|dollars)\b)",
    re.IGNORECASE,
)

#: Executive titles that anchor a compensation term the same way a name does.
EXEC_TITLES = re.compile(
    r"\b(ceo|cfo|coo|cto|cro|cmo|chief \w+ officer|executives?|vice president|vps?|founders?)\b", re.IGNORECASE
)


def number_forms(value: float) -> set[str]:
    """Spellings of a number a model might produce, all lower case."""
    forms: set[str] = set()
    n = float(value)
    if n.is_integer():
        i = int(n)
        forms |= {str(i), f"{i:,}"}
    if n >= 1_000_000:
        m = n / 1_000_000
        for s in {f"{m:g}", f"{m:.1f}", f"{m:.2f}"}:
            forms |= {f"${s}m", f"{s}m", f"${s} million", f"{s} million", f"${s}mm"}
    if n >= 1_000:
        k = n / 1_000
        for s in {f"{k:g}", f"{k:.0f}"}:
            forms |= {f"${s}k", f"{s}k"}
    return {f.lower() for f in forms if len(f) >= 3}


def _walk(obj: Any) -> Iterable[tuple[str, Any]]:
    """Yield (key, value) for every leaf of a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _walk(v)
            else:
                yield str(k), v
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


@dataclass
class SensitiveIndex:
    """Names, numbers and markers that must not appear in output, traces or logs."""

    people: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    phrases: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, restricted: RestrictedData) -> "SensitiveIndex":
        """Derive the index from the restricted HR and financial datasets."""
        idx = cls()
        for rec in restricted.hr.get("records", []):
            if rec.get("name"):
                idx.people.add(rec["name"].lower())
        # Numbers of 1,000 and above only: small values (22 months, 15%) are too
        # common in ordinary CRM answers to block on alone; the category terms catch them.
        for _key, value in [*_walk(restricted.hr), *_walk(restricted.financial)]:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 1000:
                idx.numbers |= number_forms(value)
        for key in restricted.financial.get("confidential_strategy", {}):
            idx.phrases.add(key.lower())
        return idx

    def people_in(self, text: str) -> list[tuple[int, int]]:
        """Spans of employee names found in ``text`` (already normalized)."""
        return [(m.start(), m.end()) for p in self.people for m in re.finditer(re.escape(p), text)]

    def numbers_in(self, text: str) -> list[tuple[int, int]]:
        """Spans of restricted numbers found in ``text``, matched on token boundaries."""
        spans = []
        for n in self.numbers:
            for m in re.finditer(rf"(?<![\w.,]){re.escape(n)}(?![\w]|[.,]\d)", text):
                spans.append((m.start(), m.end()))
        return spans
