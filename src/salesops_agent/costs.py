"""Token usage and estimated cost.

Cost is estimated from the token counts the model reports for each call and the
per-million-token prices in ``config/agent.yaml``. Two caveats are carried in
every record rather than hidden:

* the prices are public list prices, and the course proxy may bill differently;
* the scripted model has no real tokenizer, so its usage is estimated from text
  length and flagged with ``usage_is_estimate``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from langchain_core.messages import AIMessage


@dataclass
class Usage:
    """Summed token usage for one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def collect_usage(messages: Iterable[Any]) -> Usage:
    """Sum ``usage_metadata`` across every AI message in a run."""
    usage = Usage()
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        meta = msg.usage_metadata or {}
        usage.model_calls += 1
        usage.input_tokens += int(meta.get("input_tokens", 0))
        usage.output_tokens += int(meta.get("output_tokens", 0))
        if (msg.response_metadata or {}).get("usage_is_estimate"):
            usage.estimated = True
    return usage


def estimate_cost_usd(usage: Usage, model_name: str, pricing: dict[str, dict[str, float]]) -> float:
    """Price the usage with the configured per-million rates; 0.0 when the model is unpriced."""
    rates = pricing.get(model_name)
    if not rates:
        return 0.0
    cost = (usage.input_tokens * rates["input_per_million"] + usage.output_tokens * rates["output_per_million"])
    return round(cost / 1_000_000, 8)
