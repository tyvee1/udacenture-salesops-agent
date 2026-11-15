"""Data access for the mock CRM.

The prototype loaded every JSON file — including restricted HR and financial
data — into notebook globals that any function could read. Here the two kinds of
data are separated:

* :class:`CRMData` holds only the approved SalesOps datasets. It is what the
  CRM tools and the analysis sandbox receive.
* :class:`RestrictedData` holds the internal HR and financial files. Only the
  retained internal tools receive it, and the guardrail policy blocks those tools.

Files are read relative to the configured data directory, never the current
working directory, so the agent reads the same data wherever it is started from.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The approved SalesOps datasets: attribute name -> file name.
APPROVED_FILES = {
    "accounts": "accounts.json",
    "contacts": "contacts.json",
    "opportunities": "opportunities.json",
    "activities": "activities.json",
    "support_tickets": "support_tickets.json",
    "product_usage": "product_usage.json",
}

#: Restricted internal datasets. Never handed to CRM tools or the sandbox.
RESTRICTED_FILES = {
    "hr": "internal_hr_data.json",
    "financial": "internal_financial_data.json",
}

CLOSED_STAGES = frozenset({"Closed Won", "Closed Lost"})

_SUFFIXES = re.compile(r"\b(incorporated|inc|corp|corporation|ltd|limited|llc|co|company)\b")


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def normalize_name(value: str) -> str:
    """Lower-case, strip punctuation and legal suffixes: 'TechStart Inc.' -> 'techstart'."""
    value = re.sub(r"[^a-z0-9\s]", " ", value.lower().strip())
    value = _SUFFIXES.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


class AccountNotFound(LookupError):
    """No account matched; ``candidates`` lists near misses when the match was ambiguous."""

    def __init__(self, query: str, candidates: list[str] | None = None):
        self.query = query
        self.candidates = candidates or []
        detail = f" Did you mean one of: {', '.join(self.candidates)}?" if self.candidates else ""
        super().__init__(f"No single account matches '{query}'.{detail}")


@dataclass
class CRMData:
    """The approved SalesOps datasets, loaded once per process."""

    accounts: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    opportunities: list[dict[str, Any]]
    activities: list[dict[str, Any]]
    support_tickets: list[dict[str, Any]]
    product_usage: list[dict[str, Any]]

    @classmethod
    def load(cls, data_dir: Path) -> "CRMData":
        """Read every approved file from ``data_dir``."""
        return cls(**{attr: _load(data_dir / name) for attr, name in APPROVED_FILES.items()})

    # -- account resolution -------------------------------------------------

    def find_account(self, name_or_id: str) -> dict[str, Any]:
        """Resolve an account by exact ID, exact normalized name, unique partial match, or close typo.

        The prototype returned the *first* partial match, so an ambiguous name
        silently answered about the wrong customer. Here an ambiguous partial
        match raises :class:`AccountNotFound` listing the candidates, and the
        typo fallback requires a single close match.
        """
        key = name_or_id.strip()
        by_id = {a["account_id"].lower(): a for a in self.accounts}
        if key.lower() in by_id:
            return by_id[key.lower()]

        query = normalize_name(key)
        if not query:
            raise AccountNotFound(name_or_id)
        normalized = {normalize_name(a["name"]): a for a in self.accounts}
        if query in normalized:
            return normalized[query]

        partial = [a for n, a in normalized.items() if query in n or n in query]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise AccountNotFound(name_or_id, sorted(a["name"] for a in partial)[:5])

        close = difflib.get_close_matches(query, list(normalized), n=2, cutoff=0.82)
        if len(close) == 1:
            return normalized[close[0]]
        raise AccountNotFound(name_or_id, [normalized[c]["name"] for c in close])

    # -- related records ----------------------------------------------------

    def _for(self, rows: list[dict[str, Any]], account_id: str) -> list[dict[str, Any]]:
        return [r for r in rows if r["account_id"] == account_id]

    def contacts_for(self, account_id: str) -> list[dict[str, Any]]:
        return self._for(self.contacts, account_id)

    def opportunities_for(self, account_id: str) -> list[dict[str, Any]]:
        return self._for(self.opportunities, account_id)

    def activities_for(self, account_id: str) -> list[dict[str, Any]]:
        """Activities newest first, so a truncated list keeps the most recent."""
        return sorted(self._for(self.activities, account_id), key=lambda a: a["date"], reverse=True)

    def tickets_for(self, account_id: str) -> list[dict[str, Any]]:
        return self._for(self.support_tickets, account_id)

    def usage_for(self, account_id: str) -> list[dict[str, Any]]:
        return self._for(self.product_usage, account_id)

    def account_name(self, account_id: str) -> str:
        return next((a["name"] for a in self.accounts if a["account_id"] == account_id), account_id)

    def known_emails(self) -> set[str]:
        """Every contact address in the CRM, lower-cased (the only valid email recipients)."""
        return {c["email"].lower() for c in self.contacts}


@dataclass
class RestrictedData:
    """Internal HR and financial datasets. Loaded separately so nothing approved can reach them."""

    hr: dict[str, Any]
    financial: dict[str, Any]

    @classmethod
    def load(cls, data_dir: Path) -> "RestrictedData":
        return cls(**{attr: _load(data_dir / name) for attr, name in RESTRICTED_FILES.items()})
