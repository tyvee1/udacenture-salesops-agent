"""Version fingerprints recorded with every run.

The scenario this project answers: a week after the demo, the same prompt
produced different behaviour, and nobody could say whether a prompt edit, a tool
change, a dependency update or a configuration difference caused it.

:func:`component_versions` answers that question mechanically. Each run log and
trace carries a hash of every file that can change behaviour, plus the git
commit when one is available. Two runs that disagree can be compared field by
field, and the first differing hash names the cause.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from salesops_agent import __version__
from salesops_agent.config import Settings


def file_sha256(path: Path) -> str | None:
    """Hash a file's bytes, or return None when it does not exist."""
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit(root: Path) -> str | None:
    """Return the short commit hash of the working tree, or None outside a git checkout.

    Never raises: a reviewer running from an extracted archive (no .git) still
    gets every file hash, just no commit id.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def component_versions(settings: Settings, prompt: Any) -> dict[str, Any]:
    """Collect the version fingerprint for one run.

    ``prompt`` is the loaded :class:`~salesops_agent.prompts.PromptVersion`.
    Optional files (the guardrail policy before it exists, for example) are
    reported as ``None`` rather than omitted, so every record has the same keys.
    """
    root = settings.root
    return {
        "package_version": __version__,
        "git_commit": git_commit(root),
        "prompt_id": prompt.prompt_id,
        "prompt_version": prompt.version,
        "prompt_sha256": prompt.sha256,
        "agent_config_sha256": file_sha256(settings.config_path),
        "tool_registry_sha256": file_sha256(settings.path("tool_registry")),
        "policy_sha256": file_sha256(root / "config" / "policy.yaml"),
        "uv_lock_sha256": file_sha256(root / "uv.lock"),
        "model": f"{settings.model.provider}:{settings.model.name}",
    }
