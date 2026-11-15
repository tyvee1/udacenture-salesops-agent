"""The prototype's internal-data tools, retained so their misuse can be demonstrated and blocked.

These are the prototype's main exposure path: they ignore the query and return
the entire restricted dataset. They stay registered so the tool guardrail can be
shown blocking a real proposed call in code. In production they would be removed
from the SalesOps agent's tool surface entirely.
"""

from __future__ import annotations

from typing import Any


def lookup_internal_hr_data(ctx, query: str) -> dict[str, Any]:
    """Return the whole HR dataset (prototype behaviour)."""
    return {"query": query, "data": ctx.restricted.hr}


def lookup_internal_financial_data(ctx, query: str) -> dict[str, Any]:
    """Return the whole financial dataset (prototype behaviour)."""
    return {"query": query, "data": ctx.restricted.financial}
