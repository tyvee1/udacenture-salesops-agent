"""The sandboxed analysis tool exposed to the agent."""

from __future__ import annotations

from typing import Any

from salesops_agent.sandbox import run_analysis


def analyze_crm_data(ctx, code: str) -> dict[str, Any]:
    """Validate and run analysis code over the approved CRM datasets.

    Validation also happens in the tool guardrail before this is reached; it is
    repeated inside :func:`run_analysis` so a direct call cannot skip it.
    """
    outcome = run_analysis(code, ctx.crm, ctx.sandbox)
    return flag_empty_result(outcome)


EMPTY_RESULT_NOTE = (
    "The result is empty. An empty result usually means a filter value does not match the data "
    "(values are case-sensitive; see the field values in the tool description). Check the filter "
    "before telling the user that nothing matches."
)


def flag_empty_result(outcome: dict[str, Any]) -> dict[str, Any]:
    """Add a note when a successful analysis returned an empty collection.

    Why: a model that filters on a guessed value (``'Customer'`` for ``'customer'``)
    gets ``[]`` back and can report "there are none" as fact. In a live evaluation
    run that is exactly what happened. The note puts the doubt into the tool result
    itself, so it does not depend on the model remembering the prompt.

    Only empty containers are flagged. A count of ``0`` or ``False`` is a real
    answer and passes through unchanged, as does any error result.
    """
    result = outcome.get("result")
    if outcome.get("status") == "ok" and isinstance(result, (list, dict, tuple, str)) and len(result) == 0:
        return {**outcome, "note": EMPTY_RESULT_NOTE}
    return outcome
