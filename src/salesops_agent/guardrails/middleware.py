"""Agent middleware that enforces the tool guardrail on every proposed tool call.

LangChain calls :meth:`ToolGuardrailMiddleware.wrap_tool_call` with each tool
call the model proposes, *before* the tool runs. If the policy check fails, the
tool body is never invoked: the model receives a ``{"status": "blocked", ...}``
result instead and must answer without the data. Every decision, allowed or
blocked, is recorded on the run's :class:`~salesops_agent.recorder.RunRecorder`.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from salesops_agent.guardrails.checks import ToolCheckContext, blocked_tool_content, check_tool_call


class ToolGuardrailMiddleware(AgentMiddleware):
    """Policy check in front of every tool execution."""

    def __init__(self, check_ctx: ToolCheckContext, recorder: Any):
        super().__init__()
        self.check_ctx = check_ctx
        self.recorder = recorder

    def _decide(self, request) -> tuple[Any, ToolMessage | None]:
        call = request.tool_call
        decision = check_tool_call(call["name"], call.get("args") or {}, self.check_ctx)
        self.recorder.guardrail(decision)
        if decision.blocked:
            self.recorder.tool_event(call["name"], call.get("args") or {}, status="blocked",
                                     output=decision.blocked_payload())
            return decision, ToolMessage(
                content=blocked_tool_content(decision), tool_call_id=call["id"], name=call["name"], status="error",
            )
        return decision, None

    def _record_result(self, request, result: Any) -> Any:
        call = request.tool_call
        content = result.content if isinstance(result, ToolMessage) else result
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            parsed = content
        self.recorder.tool_event(call["name"], call.get("args") or {}, status="executed", output=parsed)
        return result

    def wrap_tool_call(self, request, handler: Callable) -> Any:
        """Block, or run the tool and record its output."""
        _decision, blocked = self._decide(request)
        if blocked is not None:
            return blocked
        return self._record_result(request, handler(request))
