"""Versioned prompt loading.

Prompts live in ``prompts/<id>/v<version>.md``. Each file starts with a YAML front
matter block (between ``---`` lines) describing the version, followed by the
prompt text the model receives. ``config/agent.yaml`` chooses which version is
active, so changing the prompt is a reviewed file change rather than an edit
buried in code — the fix for the prototype's "same question, different
behaviour, nobody knows why" problem.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from salesops_agent.config import ConfigError, Settings


@dataclass(frozen=True)
class PromptVersion:
    """One loaded prompt file: its metadata, its text and a hash of the text."""

    prompt_id: str
    version: int
    metadata: dict[str, Any]
    text: str
    path: Path

    @property
    def sha256(self) -> str:
        """Hash of the text the model actually receives (front matter excluded)."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _split_front_matter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    """Separate the ``---`` YAML header from the prompt body."""
    if not raw.startswith("---\n"):
        raise ConfigError(f"{path}: prompt file must start with a '---' front matter block")
    try:
        _, header, body = raw.split("---\n", 2)
    except ValueError as exc:
        raise ConfigError(f"{path}: front matter block is not closed with '---'") from exc
    return yaml.safe_load(header) or {}, body.strip() + "\n"


def load_prompt(settings: Settings) -> PromptVersion:
    """Load the prompt version selected in agent.yaml and check it is self-consistent.

    The file's own ``id`` and ``version`` must match its path and the config, so
    a file copied to a new version number without editing its header is caught
    instead of silently reporting the wrong version in every log.
    """
    path = settings.root / "prompts" / settings.prompt_id / f"v{settings.prompt_version}.md"
    if not path.is_file():
        raise ConfigError(f"Prompt {settings.prompt_id} v{settings.prompt_version} not found at {path}")
    meta, text = _split_front_matter(path.read_text(encoding="utf-8"), path)
    if meta.get("id") != settings.prompt_id or int(meta.get("version", -1)) != settings.prompt_version:
        raise ConfigError(
            f"{path}: front matter says id={meta.get('id')!r} version={meta.get('version')!r}, "
            f"but agent.yaml selects id={settings.prompt_id!r} version={settings.prompt_version}"
        )
    return PromptVersion(settings.prompt_id, settings.prompt_version, meta, text, path)
