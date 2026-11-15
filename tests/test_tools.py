"""Tool behaviour against the real mock CRM data.

Expected values are computed from the data files inside each test rather than
typed in, so the tests stay right if the data changes and fail if a tool drifts
from the data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from salesops_agent.config import load_settings
from salesops_agent.data import CLOSED_STAGES, AccountNotFound, CRMData
from salesops_agent.tools import ToolContext, build_tools, load_registry
from salesops_agent.tools import crm as crm_tools
from salesops_agent.tools import email as email_tools
from salesops_agent.tools.email import EmailOutbox

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def crm() -> CRMData:
    return CRMData.load(ROOT / "data")


@pytest.fixture
def ctx(crm: CRMData, tmp_path: Path) -> ToolContext:
    return ToolContext(crm=crm, restricted=None, outbox=EmailOutbox(tmp_path / "outbox.jsonl"))


@pytest.mark.parametrize("query", ["TechStart Inc", "TechStart Inc.", "techstart", "A003", "a003"])
def test_forgiving_lookup_resolves(crm: CRMData, query: str) -> None:
    assert crm.find_account(query)["account_id"] == "A003"


def test_typo_fallback_works(crm: CRMData) -> None:
    # The prototype's typo fallback called difflib without importing it.
    assert crm.find_account("GlobalRetial")["account_id"] == "A002"


def test_unknown_account_raises(crm: CRMData) -> None:
    with pytest.raises(AccountNotFound):
        crm.find_account("Definitely Not A Customer Zzz")


def test_ambiguous_partial_match_is_reported_not_guessed(crm: CRMData) -> None:
    # Pick a word shared by more than one account name, derived from the data.
    words: dict[str, set[str]] = {}
    for a in crm.accounts:
        for w in a["name"].lower().split():
            if len(w) > 3:
                words.setdefault(w, set()).add(a["name"])
    shared = next((w for w, names in sorted(words.items()) if len(names) > 1), None)
    if shared is None:
        pytest.skip("no shared name word in this dataset")
    with pytest.raises(AccountNotFound) as exc:
        crm.find_account(shared)
    assert len(exc.value.candidates) > 1


def test_open_opportunities_exclude_closed_and_report_total(ctx: ToolContext) -> None:
    out = crm_tools.list_open_opportunities(ctx, limit=5)
    expected_total = sum(1 for o in ctx.crm.opportunities if o["stage"] not in CLOSED_STAGES)
    assert out["total"] == expected_total and out["returned"] == 5
    assert all(o["stage"] not in CLOSED_STAGES for o in out["opportunities"])


def test_pipeline_summary_matches_data(ctx: ToolContext) -> None:
    out = crm_tools.summarize_pipeline(ctx)
    open_rows = [o for o in ctx.crm.opportunities if o["stage"] not in CLOSED_STAGES]
    assert out["open_pipeline"] == {"count": len(open_rows), "amount_usd": sum(o["amount_usd"] for o in open_rows)}
    assert sum(v["count"] for v in out["by_stage"].values()) == len(ctx.crm.opportunities)


def test_account_context_limits_activities(ctx: ToolContext) -> None:
    out = crm_tools.lookup_account_context(ctx, "A002")
    assert len(out["recent_activities"]) <= crm_tools.MAX_ACTIVITIES
    assert out["activities_total"] == len(ctx.crm.activities_for("A002"))


def test_draft_email_uses_primary_contact(ctx: ToolContext) -> None:
    out = email_tools.draft_email(ctx, "Acme Corp", "next steps")
    primary = next(c for c in ctx.crm.contacts_for("A001") if c["is_primary"])
    assert out["status"] == "drafted" and out["to"] == primary["email"]
    assert ctx.outbox.count() == 0  # drafting never sends


def test_registry_builds_every_declared_tool(ctx: ToolContext) -> None:
    settings = load_settings(ROOT)
    specs = load_registry(settings.path("tool_registry"))
    tools = build_tools(specs, ctx)
    assert [t.name for t in tools] == list(specs)
    out = json.loads(next(t for t in tools if t.name == "lookup_account").invoke({"account_name_or_id": "A001"}))
    assert out["account"]["name"] == "Acme Corp"


def test_schema_rejects_out_of_bounds_limit(ctx: ToolContext) -> None:
    settings = load_settings(ROOT)
    tools = {t.name: t for t in build_tools(load_registry(settings.path("tool_registry")), ctx)}
    with pytest.raises(Exception):
        tools["list_open_opportunities"].invoke({"limit": 10_000})
