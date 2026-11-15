"""Agent creation.

This module only assembles the agent from parts built elsewhere: the model, the
registered tools, the versioned prompt, and (in later layers) middleware and a
checkpointer. Keeping assembly separate from execution means a test can build
the identical agent around a scripted model.
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain.agents import create_agent


def build_agent(
    model: Any,
    tools: Sequence[Any],
    system_prompt: str,
    middleware: Sequence[Any] = (),
    checkpointer: Any = None,
):
    """Create the SalesOps agent graph.

    Args:
        model: a LangChain chat model (the configured provider, or a scripted
            model in tests and offline evaluation).
        tools: tools built from the registry.
        system_prompt: text of the active prompt version.
        middleware: runtime controls wrapped around model and tool calls.
        checkpointer: optional state store; needed to pause for approval.
    """
    return create_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=list(middleware),
        checkpointer=checkpointer,
    )
