"""Agent middleware enforcing the tool guardrail and the human approval gate on every tool call.

LangChain calls :meth:`ToolGuardrailMiddleware.wrap_tool_call` with each tool
call the model proposes, *before* the tool runs. Order of operations:

1. **Policy check.** If it fails, the tool body never runs; the model receives
   ``{"status": "blocked", "reason": ..., "policy": ...}`` instead.
2. **Approval pause** for any tool the registry marks ``risk: high``. The
   middleware calls LangGraph's ``interrupt()``, which stops the run and hands
   the pending action to the caller. Nothing executes until the caller resumes
   with a decision. Policy runs first, so a human is never asked to approve
   something the policy forbids.
3. **Execution under a grant.** On approval, the tool runs inside
   :func:`~salesops_agent.approval.grant_scope`, the only way the outbox will
   accept a message. On rejection, the tool never runs and the model is told so.

Every step is recorded on the run's :class:`~salesops_agent.recorder.RunRecorder`.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import interrupt

from salesops_agent.approval import grant_scope, normalize_decision
from salesops_agent.guardrails.checks import ToolCheckContext, blocked_tool_content, check_tool_call


class ToolGuardrailMiddleware(AgentMiddleware):
    """Policy check and approval gate in front of every tool execution."""

    def __init__(self, check_ctx: ToolCheckContext, recorder: Any):
        super().__init__()
        self.check_ctx = check_ctx
        self.recorder = recorder

    def _message(self, call: dict[str, Any], payload: dict[str, Any], status: str = "error") -> ToolMessage:
        return ToolMessage(content=json.dumps(payload), tool_call_id=call["id"], name=call["name"], status=status)

    def _record_result(self, call: dict[str, Any], result: Any) -> Any:
        content = result.content if isinstance(result, ToolMessage) else result
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            parsed = content
        status = "executed"
        if isinstance(parsed, dict) and parsed.get("status") == "blocked":
            status = "blocked"  # the tool refused internally (e.g. no approval grant, sandbox violation)
        self.recorder.tool_event(call["name"], call.get("args") or {}, status=status, output=parsed)
        return result

    def wrap_tool_call(self, request, handler: Callable) -> Any:
        """Policy check, then approval for high-risk tools, then execution."""
        call = request.tool_call
        name, args, call_id = call["name"], call.get("args") or {}, call["id"]

        decision = check_tool_call(name, args, self.check_ctx)
        self.recorder.guardrail(decision, call_id=call_id)
        if decision.blocked:
            self.recorder.tool_event(name, args, status="blocked", output=decision.blocked_payload())
            return ToolMessage(content=blocked_tool_content(decision), tool_call_id=call_id, name=name, status="error")

        spec = self.check_ctx.specs.get(name)
        if spec is not None and spec.is_high_risk:
            self.recorder.approval_requested(call_id, name, args)
            resume = interrupt({
                "type": "approval_request",
                "call_id": call_id,
                "tool": name,
                "args": args,
                "risk": spec.risk,
                "run_id": self.recorder.run_id,
            })
            verdict, source = normalize_decision(resume)
            self.recorder.approval_decided(call_id, verdict, source)
            if verdict != "approve":
                payload = {"status": "rejected", "reason": "human_rejected", "policy": "human_approval_gate",
                           "tool": name, "detail": "A human reviewer rejected this action. It was not performed."}
                self.recorder.tool_event(name, args, status="rejected", output=payload)
                return self._message(call, payload)
            with grant_scope(name, args, source):
                return self._record_result(call, handler(request))

        return self._record_result(call, handler(request))
