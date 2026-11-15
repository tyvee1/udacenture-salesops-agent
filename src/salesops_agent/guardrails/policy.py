"""Load ``config/policy.yaml`` and compile it into ready-to-use matchers.

The policy file holds every pattern and reason code; this module turns it into
compiled regular expressions once per run. A pattern that fails to compile stops
the agent at startup with the pattern named, rather than silently matching
nothing at runtime.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from salesops_agent.config import ConfigError

#: Zero-width and bidi-control characters an attacker can insert to split a keyword.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")


def normalize(text: str) -> str:
    """Prepare text for matching: Unicode NFKC, invisible characters removed, lower case, single spaces.

    NFKC folds look-alike forms (full-width letters, ligatures) to plain ones, so
    "ｃｅｏ ｂｏｎｕｓ" is matched as "ceo bonus". Curly apostrophes become straight ones.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ConfigError(f"policy.yaml {where}: invalid pattern {pattern!r}: {exc}") from exc


@dataclass(frozen=True)
class InputCategory:
    """One input-guardrail category: a reason code, its patterns and its exemptions."""

    reason: str
    patterns: tuple[re.Pattern[str], ...]
    exempt: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class OutputCategory:
    """One output-guardrail category."""

    reason: str
    terms: re.Pattern[str]
    anchor: str | None = None
    strong: re.Pattern[str] | None = None
    exempt: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class EmailClaim:
    """A kind of claim that must never appear in a customer email."""

    name: str
    pattern: re.Pattern[str]


@dataclass
class Policy:
    """The compiled guardrail policy."""

    name: str
    version: int
    input_categories: list[InputCategory]
    input_refusal: str
    denied_data_access: dict[str, str]
    restricted_field_markers: list[str]
    screen_tool_arguments: bool
    output_categories: list[OutputCategory]
    output_proximity: int
    raw_field_reason: str
    output_refusal: str
    email_reason: str
    email_claims: list[EmailClaim]
    raw: dict[str, Any] = field(default_factory=dict)


def load_policy(path: Path) -> Policy:
    """Read and compile the policy file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    inp, tools, out, email = raw["input"], raw["tools"], raw["output"], raw["email"]

    input_categories = [
        InputCategory(
            reason=c["reason"],
            patterns=tuple(_compile(p, f"input.{c['reason']}") for p in c["patterns"]),
            exempt=tuple(_compile(p, f"input.{c['reason']}.exempt") for p in c.get("exempt", [])),
        )
        for c in inp["categories"]
    ]
    output_categories = [
        OutputCategory(
            reason=c["reason"],
            terms=_compile(c["terms"], f"output.{c['reason']}"),
            anchor=c.get("anchor"),
            strong=_compile(c["strong"], f"output.{c['reason']}.strong") if c.get("strong") else None,
            exempt=tuple(_compile(p, f"output.{c['reason']}.exempt") for p in c.get("exempt", [])),
        )
        for c in out["categories"]
    ]
    for c in output_categories:
        if c.anchor not in (None, "people", "people_or_money"):
            raise ConfigError(f"policy.yaml output.{c.reason}: unknown anchor {c.anchor!r}")
    return Policy(
        name=raw["policy_name"],
        version=int(raw["policy_version"]),
        input_categories=input_categories,
        input_refusal=" ".join(inp["refusal"].split()),
        denied_data_access=dict(tools["denied_data_access"]),
        restricted_field_markers=[m.lower() for m in tools["restricted_field_markers"]],
        screen_tool_arguments=bool(tools.get("screen_arguments_with_input_categories", True)),
        output_categories=output_categories,
        output_proximity=int(out.get("proximity", 80)),
        raw_field_reason=out.get("raw_field_reason", "raw_sensitive_fields"),
        output_refusal=" ".join(out["refusal"].split()),
        email_reason=email["reason"],
        email_claims=[EmailClaim(c["name"], _compile(c["pattern"], f"email.{c['name']}")) for c in email["claims"]],
        raw=raw,
    )
