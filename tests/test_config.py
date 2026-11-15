"""Configuration, prompt-version and credential-resolution tests.

Every check here has a case where it must fire and a case where it must stay
silent, so a passing test proves the check discriminates rather than that it
never runs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from salesops_agent.config import ConfigError, find_project_root, load_settings, resolve_api_key
from salesops_agent.prompts import load_prompt
from salesops_agent.versioning import component_versions

ROOT = Path(__file__).resolve().parents[1]
KEY_VAR = "VOCAREUM_API_KEY"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway copy of the config and prompts, so tests can edit them freely."""
    for name in ("config", "prompts"):
        shutil.copytree(ROOT / name, tmp_path / name)
    monkeypatch.delenv(KEY_VAR, raising=False)
    monkeypatch.delenv("SALESOPS_HOME", raising=False)
    return tmp_path


def test_root_found_from_a_subdirectory(project: Path) -> None:
    sub = project / "prompts" / "salesops_system"
    assert find_project_root(sub) == project.resolve()


def test_root_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALESOPS_HOME", raising=False)
    with pytest.raises(ConfigError, match="config/agent.yaml"):
        find_project_root(tmp_path)


def test_active_prompt_loads_and_matches_config(project: Path) -> None:
    settings = load_settings(project)
    prompt = load_prompt(settings)
    assert prompt.version == settings.prompt_version
    assert len(prompt.sha256) == 64 and prompt.text.strip()


def test_prompt_header_that_disagrees_with_config_is_rejected(project: Path) -> None:
    settings = load_settings(project)
    path = project / "prompts" / settings.prompt_id / f"v{settings.prompt_version}.md"
    path.write_text(path.read_text().replace(f"version: {settings.prompt_version}", "version: 99"))
    with pytest.raises(ConfigError, match="front matter says"):
        load_prompt(settings)


def test_version_fingerprint_changes_when_the_prompt_changes(project: Path) -> None:
    settings = load_settings(project)
    before = component_versions(settings, load_prompt(settings))
    path = load_prompt(settings).path
    path.write_text(path.read_text() + "\nOne more instruction.\n")
    after = component_versions(settings, load_prompt(settings))
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"prompt_sha256"}


def _write_env(project: Path, value: str) -> None:
    (project / ".env").write_text(f"{KEY_VAR}={value}\n")


def test_key_from_env_file(project: Path) -> None:
    _write_env(project, "voc-aaaa1111")
    key = resolve_api_key(load_settings(project))
    assert key.source == ".env" and key.fingerprint == "…1111"


def test_key_from_shell(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_VAR, "voc-bbbb2222")
    assert resolve_api_key(load_settings(project)).source == "shell environment"


def test_same_key_in_both_is_accepted(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(project, "voc-same9999")
    monkeypatch.setenv(KEY_VAR, "voc-same9999")
    assert resolve_api_key(load_settings(project)).fingerprint == "…9999"


def test_conflicting_keys_are_refused(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(project, "voc-aaaa1111")
    monkeypatch.setenv(KEY_VAR, "voc-bbbb2222")
    with pytest.raises(ConfigError, match="DIFFERENT values"):
        resolve_api_key(load_settings(project))


def test_placeholder_key_is_refused(project: Path) -> None:
    _write_env(project, "voc-replace-me")
    with pytest.raises(ConfigError, match="placeholder"):
        resolve_api_key(load_settings(project))


def test_missing_key_is_refused(project: Path) -> None:
    with pytest.raises(ConfigError, match="is not set"):
        resolve_api_key(load_settings(project))
