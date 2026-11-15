"""The sandboxed analysis tool exposed to the agent."""

from __future__ import annotations

from typing import Any

from salesops_agent.sandbox import run_analysis


def analyze_crm_data(ctx, code: str) -> dict[str, Any]:
    """Validate and run analysis code over the approved CRM datasets.

    Validation also happens in the tool guardrail before this is reached; it is
    repeated inside :func:`run_analysis` so a direct call cannot skip it.
    """
    return run_analysis(code, ctx.crm, ctx.sandbox)
