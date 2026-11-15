"""Input schemas for every tool, kept apart from the tool implementations.

The model sees these schemas as the tools' parameter definitions, and LangChain
validates every proposed call against them before the tool body runs. Bounds
(string lengths, limits) are part of the contract: a request for ten thousand
rows or a 50 KB "account name" is rejected at the schema rather than handled by
each tool.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AccountRef(BaseModel):
    """Identify one account by ID or by name."""

    account_name_or_id: str = Field(
        ..., min_length=1, max_length=120,
        description="Account ID such as A003, or an account name such as 'TechStart Inc'.",
    )


class OpenOpportunitiesArgs(BaseModel):
    """Arguments for listing open opportunities."""

    stage: str | None = Field(
        None, max_length=40,
        description="Optional stage filter, e.g. 'Negotiation' or 'Stalled'. Omit for all open stages.",
    )
    limit: int = Field(25, ge=1, le=100, description="Maximum rows to return (1-100).")


class NoArgs(BaseModel):
    """For tools that take no arguments."""


class DraftEmailArgs(BaseModel):
    """Arguments for drafting a follow-up email."""

    account_name_or_id: str = Field(..., min_length=1, max_length=120, description="Account ID or name.")
    purpose: str = Field(
        ..., min_length=1, max_length=500,
        description="What the follow-up is about, e.g. 'next steps on the renewal'.",
    )


class SendEmailArgs(BaseModel):
    """Arguments for sending an email. Bounded so a runaway body cannot be sent."""

    to: str = Field(..., min_length=3, max_length=254, description="Recipient address from the CRM.")
    subject: str = Field(..., min_length=1, max_length=200, description="Subject line.")
    body: str = Field(..., min_length=1, max_length=5000, description="Plain-text body.")


class AnalyzeArgs(BaseModel):
    """Code for the sandboxed analysis tool."""

    code: str = Field(
        ..., min_length=1, max_length=4000,
        description="Python statements over the approved datasets; assign the answer to `result`.",
    )


class InternalQueryArgs(BaseModel):
    """Free-text query for the retained internal-data tools."""

    query: str = Field(..., min_length=1, max_length=300, description="What to look up.")


#: Map from the registry's implementation key to its argument schema.
ARG_SCHEMAS: dict[str, type[BaseModel]] = {
    "crm.lookup_account": AccountRef,
    "crm.lookup_account_context": AccountRef,
    "crm.list_open_opportunities": OpenOpportunitiesArgs,
    "crm.summarize_pipeline": NoArgs,
    "email.draft_email": DraftEmailArgs,
    "email.send_email": SendEmailArgs,
    "analysis.analyze_crm_data": AnalyzeArgs,
    "internal.lookup_internal_hr_data": InternalQueryArgs,
    "internal.lookup_internal_financial_data": InternalQueryArgs,
}
