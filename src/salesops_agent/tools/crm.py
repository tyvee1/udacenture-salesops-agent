"""CRM read tools: accounts, account context, open opportunities, pipeline summary.

Each function takes the :class:`~salesops_agent.tools.ToolContext` first and the
model-supplied arguments after it; :mod:`salesops_agent.tools` binds the context.
Results are plain dicts that the registry serializes to JSON for the model.

Compared with the prototype:

* lookups go through :meth:`CRMData.find_account`, which reports ambiguity
  instead of silently choosing the first partial match;
* list results are compact rows with a ``limit`` and a ``total``, because
  returning every field of 150+ opportunities costs tokens and money on every
  call;
* the pipeline summary separates open from closed stages, so "pipeline value"
  no longer includes deals that were already won or lost.
"""

from __future__ import annotations

from typing import Any

from salesops_agent.data import CLOSED_STAGES, AccountNotFound

#: Recent activities returned per account; older ones are counted, not listed.
MAX_ACTIVITIES = 10


def lookup_account(ctx, account_name_or_id: str) -> dict[str, Any]:
    """Return one account record, or an error naming close candidates."""
    try:
        return {"account": ctx.crm.find_account(account_name_or_id)}
    except AccountNotFound as exc:
        return {"error": str(exc), "candidates": exc.candidates}


def lookup_account_context(ctx, account_name_or_id: str) -> dict[str, Any]:
    """Return the account plus its contacts, opportunities, recent activity, tickets and usage."""
    try:
        account = ctx.crm.find_account(account_name_or_id)
    except AccountNotFound as exc:
        return {"error": str(exc), "candidates": exc.candidates}
    aid = account["account_id"]
    activities = ctx.crm.activities_for(aid)
    return {
        "account": account,
        "contacts": ctx.crm.contacts_for(aid),
        "opportunities": ctx.crm.opportunities_for(aid),
        "recent_activities": activities[:MAX_ACTIVITIES],
        "activities_total": len(activities),
        "support_tickets": ctx.crm.tickets_for(aid),
        "product_usage": ctx.crm.usage_for(aid),
    }


def list_open_opportunities(ctx, stage: str | None = None, limit: int = 25) -> dict[str, Any]:
    """List open opportunities as compact rows, largest first."""
    rows = [o for o in ctx.crm.opportunities if o["stage"] not in CLOSED_STAGES]
    if stage:
        rows = [o for o in rows if o["stage"].lower() == stage.strip().lower()]
    rows.sort(key=lambda o: o["amount_usd"], reverse=True)
    compact = [
        {
            "opportunity_id": o["opportunity_id"],
            "account_id": o["account_id"],
            "account_name": ctx.crm.account_name(o["account_id"]),
            "name": o["name"],
            "type": o["type"],
            "stage": o["stage"],
            "amount_usd": o["amount_usd"],
            "probability": o["probability"],
            "close_date": o["close_date"],
        }
        for o in rows[:limit]
    ]
    return {"total": len(rows), "returned": len(compact), "stage_filter": stage, "opportunities": compact}


def summarize_pipeline(ctx) -> dict[str, Any]:
    """Count and value by stage, with open and closed totals reported separately."""
    by_stage: dict[str, dict[str, Any]] = {}
    for opp in ctx.crm.opportunities:
        entry = by_stage.setdefault(opp["stage"], {"count": 0, "amount_usd": 0})
        entry["count"] += 1
        entry["amount_usd"] += opp["amount_usd"]
    open_stages = {s: v for s, v in by_stage.items() if s not in CLOSED_STAGES}
    return {
        "by_stage": dict(sorted(by_stage.items())),
        "open_pipeline": {
            "count": sum(v["count"] for v in open_stages.values()),
            "amount_usd": sum(v["amount_usd"] for v in open_stages.values()),
        },
        "closed": {s: by_stage[s] for s in sorted(CLOSED_STAGES) if s in by_stage},
    }
