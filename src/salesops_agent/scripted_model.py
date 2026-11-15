"""A deterministic stand-in for the chat model.

Tests and the offline evaluation mode drive the real agent — real tools, real
guardrails, real approval gate — with only the model's decisions scripted. That
makes control behaviour reproducible without an API key or network access, and
it is the only way to test a control against a model that misbehaves on purpose
(for example, one that proposes a forbidden tool call or writes a leak).

Each scripted turn is either an ``AIMessage`` or a function that receives the
messages so far and returns one, so a turn can quote a tool result it has just
seen. Token usage is estimated from text length (about four characters per
token) and marked as an estimate, so cost reporting still has a number to work
with.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

Turn = AIMessage | Callable[[list[BaseMessage]], AIMessage]

#: Rough characters-per-token ratio used for estimated usage.
CHARS_PER_TOKEN = 4


def _text_of(messages: Sequence[BaseMessage]) -> str:
    return "".join(m.content if isinstance(m.content, str) else str(m.content) for m in messages)


class ScriptedChatModel(BaseChatModel):
    """Replay a fixed list of turns; repeat the last one if the agent asks for more."""

    turns: list[Any] = Field(default_factory=list)
    model_name: str = "scripted"
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        """Tool binding is a no-op: the script already decides which tools to call."""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self.turns:
            raise RuntimeError("ScriptedChatModel has no turns to play")
        turn = self.turns[min(len(self.calls) - 1, len(self.turns) - 1)]
        message = turn(list(messages)) if callable(turn) else turn
        message = message.model_copy()
        in_tokens = max(1, len(_text_of(messages)) // CHARS_PER_TOKEN)
        out_text = (message.content or "") + "".join(str(tc.get("args")) for tc in message.tool_calls)
        out_tokens = max(1, len(out_text) // CHARS_PER_TOKEN)
        message.usage_metadata = {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        }
        message.response_metadata = {**message.response_metadata, "model_name": self.model_name,
                                     "usage_is_estimate": True}
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_call(name: str, args: dict[str, Any], call_id: str | None = None) -> AIMessage:
    """Build a turn in which the model proposes one tool call."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id or f"call_{name}"}])


def answer(text: str) -> AIMessage:
    """Build a turn in which the model gives its final answer."""
    return AIMessage(content=text)
