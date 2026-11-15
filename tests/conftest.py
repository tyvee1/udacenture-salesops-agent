"""Shared fixtures.

Any test that runs the agent end to end uses ``project``: a throwaway copy of
the project's configuration, prompts, data and task file in a temp directory.
Runs then write their logs, traces, reports and state there, so the test suite
never adds to the repository's own ``logs/``, ``traces/`` or ``reports/``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from salesops_agent.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings for an isolated copy of the project."""
    for name in ("config", "prompts", "data", "evals", "schemas"):
        shutil.copytree(ROOT / name, tmp_path / name)
    shutil.copy(ROOT / "uv.lock", tmp_path / "uv.lock")
    monkeypatch.delenv("VOCAREUM_API_KEY", raising=False)
    monkeypatch.delenv("SALESOPS_HOME", raising=False)
    return load_settings(tmp_path)


@pytest.fixture(autouse=True)
def _guard_repository_outputs():
    """Fail any test that writes into the repository's own runtime directories."""
    def snapshot():
        return {p: p.stat().st_mtime for d in ("logs", "traces", "reports", "state")
                for p in (ROOT / d).glob("*") if p.name != ".gitkeep"} if ROOT.exists() else {}
    before = snapshot()
    yield
    after = snapshot()
    changed = sorted(str(p.relative_to(ROOT)) for p in after if before.get(p) != after[p])
    assert not changed, f"test wrote into the repository: {changed}"
