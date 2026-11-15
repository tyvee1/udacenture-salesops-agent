"""Runtime guardrails: input, tool/action and output controls enforced in code.

* :func:`check_input` — before the agent runs (no model call is made for a blocked request).
* :class:`ToolGuardrailMiddleware` — before every tool execution, inside the agent loop.
* :func:`check_output` — on the final answer, before it is returned.

The policy lives in ``config/policy.yaml``; the sensitive names and figures are
derived from the restricted data files at startup.
"""

from salesops_agent.guardrails.checks import (
    Decision,
    ToolCheckContext,
    check_email_content,
    check_input,
    check_output,
    check_tool_call,
)
from salesops_agent.guardrails.middleware import ToolGuardrailMiddleware
from salesops_agent.guardrails.policy import Policy, load_policy, normalize
from salesops_agent.guardrails.sensitive import SensitiveIndex

__all__ = [
    "Decision", "Policy", "SensitiveIndex", "ToolCheckContext", "ToolGuardrailMiddleware",
    "check_email_content", "check_input", "check_output", "check_tool_call", "load_policy", "normalize",
]
