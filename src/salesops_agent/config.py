"""Configuration loading, project-root discovery and model construction.

Everything that changes the agent's behaviour lives in a versioned file under
``config/`` or ``prompts/`` rather than in code. This module is the single place
that reads those files, so the rest of the package receives an already-validated
:class:`Settings` object.

Credentials
-----------
The course supplies a model key, kept in a project-local ``.env`` file that git
ignores. ``python-dotenv`` normally lets a variable already exported in the shell
silently win over ``.env``. That is how a machine that also does real work ends
up calling a model with the wrong key, so :func:`resolve_api_key` refuses to
guess: when the shell and ``.env`` both define the key with different values, it
stops and says so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

#: Environment variable that overrides project-root discovery (used by Docker and CI).
HOME_ENV = "SALESOPS_HOME"


class ConfigError(RuntimeError):
    """Raised when configuration or credentials are missing or contradictory."""


def find_project_root(start: Path | None = None) -> Path:
    """Return the directory holding ``config/agent.yaml``.

    Resolution order:

    1. ``$SALESOPS_HOME`` if set.
    2. The nearest ancestor of ``start`` (default: the current directory) that
       contains ``config/agent.yaml``.

    Raises :class:`ConfigError` rather than falling back to a guess, because a
    wrong root means reading the wrong prompt and writing logs somewhere else.
    """
    env_home = os.environ.get(HOME_ENV)
    if env_home:
        root = Path(env_home).expanduser().resolve()
        if not (root / "config" / "agent.yaml").is_file():
            raise ConfigError(f"{HOME_ENV}={env_home} does not contain config/agent.yaml")
        return root
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "agent.yaml").is_file():
            return candidate
    raise ConfigError(
        "Could not find config/agent.yaml from the current directory. "
        "Run commands from the project root, or set SALESOPS_HOME."
    )


@dataclass(frozen=True)
class ModelSettings:
    """Model selection and call limits from the ``model:`` block of agent.yaml."""

    provider: str
    name: str
    temperature: float
    timeout_seconds: float
    max_retries: int
    base_url: str | None
    api_key_env: str


@dataclass(frozen=True)
class Settings:
    """Validated view of ``config/agent.yaml`` with paths resolved against the root."""

    root: Path
    raw: dict[str, Any]
    model: ModelSettings
    prompt_id: str
    prompt_version: int
    recursion_limit: int
    pricing: dict[str, dict[str, float]]
    paths: dict[str, Path] = field(default_factory=dict)

    @property
    def config_path(self) -> Path:
        """Path of the agent configuration file itself (hashed into every run record)."""
        return self.root / "config" / "agent.yaml"

    def path(self, key: str) -> Path:
        """Return a configured path such as ``data_dir`` or ``logs_dir``."""
        try:
            return self.paths[key]
        except KeyError as exc:  # pragma: no cover - programming error, not user error
            raise ConfigError(f"paths.{key} is not configured in agent.yaml") from exc


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    """Fetch a required key or raise a ConfigError naming the file section."""
    if key not in mapping:
        raise ConfigError(f"agent.yaml: missing '{key}' in {where}")
    return mapping[key]


def load_settings(root: Path | None = None) -> Settings:
    """Load and validate ``config/agent.yaml``.

    Paths in the ``paths:`` block are resolved relative to the project root, so
    the agent behaves the same whichever directory the user runs it from.
    Defaults are supplied for the runtime directories (logs, traces, reports,
    state) so older config files keep working.
    """
    root = root or find_project_root()
    cfg_path = root / "config" / "agent.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    agent = _require(raw, "agent", "top level")
    prompt = _require(agent, "prompt", "agent")
    model = _require(raw, "model", "top level")
    provider = _require(model, "provider", "model")
    providers = _require(raw, "providers", "top level")
    if provider not in providers:
        raise ConfigError(f"agent.yaml: model.provider '{provider}' has no entry under providers")
    provider_cfg = providers[provider]

    model_settings = ModelSettings(
        provider=provider,
        name=_require(model, "name", "model"),
        temperature=float(model.get("temperature", 0.0)),
        timeout_seconds=float(model.get("timeout_seconds", 60)),
        max_retries=int(model.get("max_retries", 2)),
        base_url=provider_cfg.get("base_url"),
        api_key_env=_require(provider_cfg, "api_key_env", f"providers.{provider}"),
    )

    path_defaults = {
        "data_dir": "data",
        "tool_registry": "config/tools.yaml",
        "logs_dir": "logs",
        "traces_dir": "traces",
        "reports_dir": "reports",
        "state_dir": "state",
    }
    configured = {**path_defaults, **(raw.get("paths") or {})}
    paths = {key: (root / value).resolve() for key, value in configured.items() if value}

    return Settings(
        root=root,
        raw=raw,
        model=model_settings,
        prompt_id=_require(prompt, "id", "agent.prompt"),
        prompt_version=int(_require(prompt, "version", "agent.prompt")),
        recursion_limit=int(agent.get("recursion_limit", 25)),
        pricing=raw.get("pricing") or {},
        paths=paths,
    )


@dataclass(frozen=True)
class ResolvedKey:
    """An API key plus where it came from. ``fingerprint`` is safe to print; ``value`` is not."""

    env_var: str
    source: str  # ".env" or "shell environment"
    value: str

    @property
    def fingerprint(self) -> str:
        """Last four characters only — enough to tell two keys apart, never enough to use one."""
        return f"…{self.value[-4:]}" if len(self.value) >= 8 else "…(short)"


def resolve_secret(root: Path, var: str, hint: str) -> ResolvedKey:
    """Find a secret in ``.env`` or the shell, refusing on any ambiguity.

    * Only in ``.env``           -> use it.
    * Only in the shell          -> use it.
    * In both, same value        -> use it (reported as ``.env``).
    * In both, different values  -> raise :class:`ConfigError`. Guessing which one
      the user meant is exactly the accident this check exists to prevent.
    * Still the ``.env.example`` placeholder, or in neither -> raise with ``hint``.
    """
    env_file = root / ".env"
    from_file = (dotenv_values(env_file).get(var) or "").strip() if env_file.is_file() else ""
    from_shell = (os.environ.get(var) or "").strip()

    for label, value in ((".env", from_file), ("shell environment", from_shell)):
        if "replace-me" in value:
            raise ConfigError(f"{var} in the {label} is still the .env.example placeholder. {hint}")
    if from_file and from_shell and from_file != from_shell:
        raise ConfigError(
            f"{var} is set in both .env and the shell environment with DIFFERENT values. "
            f"Refusing to guess which to use. Run `unset {var}` to use .env, "
            f"or remove it from .env to use the shell value."
        )
    if from_file:
        return ResolvedKey(var, ".env", from_file)
    if from_shell:
        return ResolvedKey(var, "shell environment", from_shell)
    raise ConfigError(f"{var} is not set. {hint}")


def resolve_api_key(settings: Settings) -> ResolvedKey:
    """Resolve the model key for the configured provider (see :func:`resolve_secret`)."""
    return resolve_secret(
        settings.root, settings.model.api_key_env,
        f"Copy .env.example to .env and paste the course-provided key (provider '{settings.model.provider}').",
    )


def make_chat_model(settings: Settings):
    """Build the chat model named in agent.yaml.

    Both supported providers speak the OpenAI chat-completions protocol, so one
    client class covers them; the provider only changes the base URL and the
    environment variable holding the key. Temperature 0 and an explicit timeout
    and retry count make runs as repeatable as a hosted model allows.
    """
    from langchain_openai import ChatOpenAI  # imported lazily: blocked requests never need it

    key = resolve_api_key(settings)
    kwargs: dict[str, Any] = {
        "model": settings.model.name,
        "temperature": settings.model.temperature,
        "timeout": settings.model.timeout_seconds,
        "max_retries": settings.model.max_retries,
        "api_key": key.value,
    }
    if settings.model.base_url:
        kwargs["base_url"] = settings.model.base_url
    return ChatOpenAI(**kwargs)
