"""Optional external observability: send runs to Langfuse, redacted.

Off by default. Enabled per run with ``salesops-agent ask ... --langfuse`` after
``uv sync --extra observability`` and setting ``LANGFUSE_PUBLIC_KEY``,
``LANGFUSE_SECRET_KEY`` and ``LANGFUSE_HOST`` in ``.env`` (a self-hosted Langfuse
at ``http://localhost:3000``, or Langfuse Cloud). The local logs, traces and
monitoring report never depend on it.

Masking
-------
Langfuse calls the ``mask`` hook with whatever LangChain hands it: plain strings
and dicts, but also message objects and tuples. A mask that only handles
strings and dicts silently passes those through unredacted. :func:`make_mask`
first converts anything to plain JSON-compatible data, then applies the same
:class:`~salesops_agent.redaction.Redactor` the local traces use, and fails
closed: if conversion or redaction raises, Langfuse receives a redaction
marker, never the raw value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from salesops_agent.config import ConfigError, Settings, resolve_secret
from salesops_agent.redaction import Redactor

HINT = "Add LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST to .env (see .env.example)."


def to_plain(value: Any) -> Any:
    """Convert LangChain messages, pydantic models, tuples and sets into dicts, lists and strings."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_plain(v) for v in value]
    content = getattr(value, "content", None)
    if content is not None and hasattr(value, "type"):  # a LangChain message
        out: dict[str, Any] = {"type": value.type, "content": to_plain(content)}
        if getattr(value, "tool_calls", None):
            out["tool_calls"] = to_plain(value.tool_calls)
        if getattr(value, "name", None):
            out["name"] = value.name
        return out
    if hasattr(value, "model_dump"):
        return to_plain(value.model_dump())
    return str(value)


def make_mask(redactor: Redactor) -> Callable[..., Any]:
    """Build the ``mask(data=...)`` hook Langfuse calls before sending anything."""

    def mask(*, data: Any, **_: Any) -> Any:
        try:
            return redactor.obj(to_plain(data))
        except Exception:  # fail closed: never send the raw value because masking broke
            return "[REDACTED: mask error]"

    return mask


@dataclass
class LangfuseSession:
    """Callbacks to pass into a run, plus the client to flush when the run is done."""

    callbacks: list[Any]
    client: Any

    def flush(self) -> None:
        """Send any buffered events before the process exits."""
        self.client.flush()


def langfuse_session(settings: Settings, **client_kwargs: Any) -> LangfuseSession:
    """Build the masked Langfuse client and the LangChain callback that feeds it.

    Raises :class:`ConfigError` with an actionable message if the extra is not
    installed or a key is missing or contradictory. ``client_kwargs`` pass through
    to the Langfuse client (the tests use them to capture spans instead of sending).
    """
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        raise ConfigError("Langfuse is not installed. Run: uv sync --extra observability") from exc

    from salesops_agent.data import RestrictedData
    from salesops_agent.guardrails import SensitiveIndex, load_policy
    from salesops_agent.versioning import git_commit

    public = resolve_secret(settings.root, "LANGFUSE_PUBLIC_KEY", HINT).value
    secret = resolve_secret(settings.root, "LANGFUSE_SECRET_KEY", HINT).value
    host = resolve_secret(settings.root, "LANGFUSE_HOST", HINT).value
    policy = load_policy(settings.root / "config" / "policy.yaml")
    redactor = Redactor(policy, SensitiveIndex.build(RestrictedData.load(settings.path("data_dir"))))
    client = Langfuse(public_key=public, secret_key=secret, host=host, mask=make_mask(redactor),
                      environment="local", release=git_commit(settings.root) or "unversioned", **client_kwargs)
    return LangfuseSession(callbacks=[CallbackHandler(public_key=public)], client=client)
