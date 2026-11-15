"""Tool registry: builds the agent's tools from ``config/tools.yaml``.

The YAML file is the source of truth for which tools exist, their versions,
risk levels and data access. This module only binds each declared
implementation to its argument schema and to the shared :class:`ToolContext`.
A tool missing from the YAML cannot be called, whatever the model proposes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml
from langchain_core.tools import StructuredTool

from salesops_agent.config import ConfigError, Settings
from salesops_agent.data import CRMData, RestrictedData
from salesops_agent.schemas import ARG_SCHEMAS
from salesops_agent.sandbox import SandboxConfig
from salesops_agent.tools import analysis, crm, email, internal
from salesops_agent.tools.email import EmailOutbox

_MODULES = {"analysis": analysis, "crm": crm, "email": email, "internal": internal}


@dataclass
class ToolContext:
    """Everything a tool may touch, passed explicitly instead of read from globals."""

    crm: CRMData
    restricted: RestrictedData
    outbox: EmailOutbox
    sandbox: SandboxConfig | None = None


@dataclass(frozen=True)
class ToolSpec:
    """One entry from the registry file."""

    name: str
    version: int
    implementation: str
    data_access: str
    risk: str
    description: str

    @property
    def is_high_risk(self) -> bool:
        return self.risk == "high"


def load_registry(path: Path) -> dict[str, ToolSpec]:
    """Parse and validate the tool registry. Unknown implementations are a config error."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs: dict[str, ToolSpec] = {}
    for entry in raw.get("tools", []):
        spec = ToolSpec(
            name=entry["name"],
            version=int(entry["version"]),
            implementation=entry["implementation"],
            data_access=entry["data_access"],
            risk=entry["risk"],
            description=" ".join(str(entry["description"]).split()),
        )
        if spec.name in specs:
            raise ConfigError(f"{path}: tool '{spec.name}' is declared twice")
        if spec.implementation not in ARG_SCHEMAS:
            raise ConfigError(f"{path}: tool '{spec.name}' names unknown implementation '{spec.implementation}'")
        specs[spec.name] = spec
    return specs


def _resolve(implementation: str) -> Callable[..., dict[str, Any]]:
    module_name, func_name = implementation.split(".", 1)
    return getattr(_MODULES[module_name], func_name)


def build_context(settings: Settings) -> ToolContext:
    """Load the data and outbox the tools operate on."""
    data_dir = settings.path("data_dir")
    return ToolContext(
        crm=CRMData.load(data_dir),
        restricted=RestrictedData.load(data_dir),
        outbox=EmailOutbox(settings.path("state_dir") / "email_outbox.jsonl"),
        sandbox=SandboxConfig.load(settings.root / "config" / "sandbox.yaml"),
    )


def build_tools(specs: dict[str, ToolSpec], ctx: ToolContext) -> list[StructuredTool]:
    """Turn registry entries into LangChain tools bound to ``ctx``.

    Each tool returns JSON text, so what the model reads, what the trace records
    and what the guardrails inspect are the same string.
    """
    tools = []
    for spec in specs.values():
        func = _resolve(spec.implementation)

        def run(*, _func=func, **kwargs: Any) -> str:
            return json.dumps(_func(ctx, **kwargs), default=str)

        tools.append(
            StructuredTool.from_function(
                func=run,
                name=spec.name,
                description=spec.description,
                args_schema=ARG_SCHEMAS[spec.implementation],
            )
        )
    return tools
