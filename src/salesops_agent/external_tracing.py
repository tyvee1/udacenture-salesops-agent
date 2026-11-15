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

Trace shape
-----------
One run of the agent is one Langfuse trace named ``salesops-ask``, even when the
run pauses for an email approval and the agent is invoked twice::

    answer-salesops-question   [agent]      input: user message; output: the answer the user saw
    ├── check-input            [guardrail]  allowed or blocked, with reason and policy
    ├── LangGraph              [chain]      from the LangChain integration
    │   ├── model / ChatOpenAI [generation] model name and token usage
    │   ├── tools / <tool>     [tool]
    │   └── ...
    ├── check-tool-call        [guardrail]  one per tool call the policy screened
    ├── require-human-approval [guardrail]  the paused action and the decision
    └── check-output           [guardrail]

Trace attributes come from ``propagate_attributes``: user id, session id (only
when the caller named a session, since a one-off question has no conversation to
group), tags, the prompt version as the trace ``version``, and metadata linking
the trace to the local ``run_id`` and ``traces/<run_id>.json``. A human approval
or rejection is also recorded as a ``human-approval`` boolean score.

Masking fails closed; tracing fails open. If the mask errors, Langfuse receives a
marker. If tracing itself errors, the run's outcome is unchanged: the recorder
ignores observer exceptions, and the tracer reports the problem on stderr.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

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


def make_mask(redactor: Redactor, passthrough: frozenset[str] = frozenset()) -> Callable[..., Any]:
    """Build the ``mask(data=...)`` hook Langfuse calls before sending anything.

    ``passthrough`` holds exact strings known not to be sensitive — the active system
    prompt. The prompt says "never state ... acquisition plans", so the topic rules would
    otherwise blank the whole prompt in every generation, hiding the instructions the
    model was following. Only an exact match passes: a string that contains the prompt
    plus anything else is redacted as usual.
    """

    sentinel = "salesops-system-prompt-placeholder"

    def swap(value: Any, old: Any, new: Any) -> Any:
        if isinstance(value, str):
            return new if value == old else value
        if isinstance(value, dict):
            return {k: swap(v, old, new) for k, v in value.items()}
        if isinstance(value, list):
            return [swap(v, old, new) for v in value]
        return value

    def scrub(value: Any) -> Any:
        # The whole structure still goes through the redactor, so its key rules apply.
        for text in passthrough:
            value = swap(value, text, sentinel)
        value = redactor.obj(value)
        for text in passthrough:
            value = swap(value, sentinel, text)
        return value

    def mask(*, data: Any, **_: Any) -> Any:
        try:
            return scrub(to_plain(data))
        except Exception:  # fail closed: never send the raw value because masking broke
            return "[REDACTED: mask error]"

    return mask


#: Stable names: renaming one breaks saved filters, dashboards and evaluators in Langfuse.
TRACE_NAME = "salesops-ask"
ROOT_NAME = "answer-salesops-question"
GUARDRAIL_NAMES = {"input": "check-input", "tool": "check-tool-call", "output": "check-output"}
APPROVAL_NAME = "require-human-approval"
APPROVAL_SCORE = "human-approval"

#: Guardrail fields safe and useful to show. ``matched`` and ``detail`` can quote the
#: restricted text that triggered the block, so they stay in the local trace only.
GUARDRAIL_FIELDS = ("layer", "action", "policy", "reason", "tool")


@dataclass
class RunInfo:
    """What the tracer needs to label one run's trace."""

    run_id: str
    question: str
    user_id: str
    session_id: str | None  # None for a one-off question
    thread_key: str  # user::session::thread, the memory isolation key
    prompt_version: int
    model: str
    task_id: str | None = None
    source: str = "cli"


class NullTracer:
    """The default: no external tracing. Every hook is a no-op."""

    @contextlib.contextmanager
    def trace(self, info: RunInfo) -> Iterator[None]:
        yield

    def callbacks(self) -> list[Any]:
        return []

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        return None

    def end(self, result: Any) -> None:
        return None


@dataclass
class LangfuseTracer:
    """Builds one Langfuse trace per run from the run's own lifecycle and recorder events.

    The agent service calls :meth:`trace` around each start or resume, :meth:`callbacks`
    inside it (so the LangChain handler nests under the root observation), and
    :meth:`end` with the result. Guardrail and approval decisions arrive through
    :meth:`on_event`, registered as a :class:`~salesops_agent.recorder.RunRecorder` listener.
    """

    client: Any
    public_key: str
    _root: Any = field(default=None, repr=False)
    _pending: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _warned: bool = field(default=False, repr=False)

    def _warn(self, what: str, exc: Exception) -> None:
        if not self._warned:
            print(f"[langfuse] tracing problem ({what}: {type(exc).__name__}); the run itself is unaffected",
                  file=sys.stderr)
            self._warned = True

    @contextlib.contextmanager
    def trace(self, info: RunInfo) -> Iterator[None]:
        """Open the root observation and propagate the trace attributes for one run."""
        from langfuse import propagate_attributes

        metadata = {"run_id": info.run_id, "local_trace": f"traces/{info.run_id}.json",
                    "thread_key": info.thread_key, "model": info.model, "source": info.source}
        if info.task_id:
            metadata["task_id"] = info.task_id
        attributes: dict[str, Any] = dict(
            trace_name=TRACE_NAME, user_id=info.user_id, version=f"prompt-v{info.prompt_version}",
            tags=["salesops-agent", info.source], metadata={k: str(v)[:200] for k, v in metadata.items()},
        )
        if info.session_id:
            attributes["session_id"] = info.session_id
        with contextlib.ExitStack() as stack:
            try:  # fail open: if the trace cannot start, the run goes ahead untraced
                self._root = stack.enter_context(self.client.start_as_current_observation(
                    as_type="agent", name=ROOT_NAME, input=[{"role": "user", "content": info.question}]))
                stack.enter_context(propagate_attributes(**attributes))
            except Exception as exc:
                self._warn("start of trace", exc)
                stack.close()
                self._root = None
            try:
                yield
            finally:
                self._root = None

    def callbacks(self) -> list[Any]:
        """A LangChain handler created inside the active trace, so its spans nest under the root."""
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler(public_key=self.public_key)]

    def _observe(self, name: str, *, input: Any, output: Any, blocked: bool, message: str | None) -> None:
        with self.client.start_as_current_observation(
            as_type="guardrail", name=name, input=input, output=output,
            level="WARNING" if blocked else "DEFAULT", status_message=message,
        ):
            pass

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        """Turn a recorder event into a guardrail observation or an approval record."""
        try:
            if kind == "guardrail":
                layer = data.get("layer", "unknown")
                shown = {k: data[k] for k in GUARDRAIL_FIELDS if k in data}
                blocked = data.get("action") == "blocked"
                self._observe(GUARDRAIL_NAMES.get(layer, f"check-{layer}"), input={"layer": layer, "tool": data.get("tool")},
                              output=shown, blocked=blocked, message=data.get("reason") if blocked else None)
            elif kind == "approval_requested":
                self._pending[data["call_id"]] = {"tool": data.get("tool"), "args": data.get("args")}
            elif kind == "approval_decided":
                action = self._pending.pop(data["call_id"], {"tool": None, "args": None})
                rejected = data.get("decision") != "approve"
                self._observe(APPROVAL_NAME, input=action,
                              output={"decision": data.get("decision"), "source": data.get("source")},
                              blocked=rejected, message=f"rejected ({data.get('source')})" if rejected else None)
                self.client.score_current_trace(
                    name=APPROVAL_SCORE, value=0 if rejected else 1, data_type="BOOLEAN",
                    comment=f"{data.get('decision')} via {data.get('source')} for {action.get('tool')}",
                )
        except Exception as exc:  # fail open: tracing must not change the run
            self._warn(f"event {kind}", exc)

    def end(self, result: Any) -> None:
        """Set the root output to what the user saw, and mark blocked or failed runs."""
        try:
            for call_id, action in list(self._pending.items()):  # paused and handed to a person (UI)
                self._observe(APPROVAL_NAME, input=action, output={"decision": "pending"}, blocked=False,
                              message="awaiting a person's decision")
                self._pending.pop(call_id)
            if self._root is None:
                return
            level = {"error": "ERROR", "blocked": "WARNING", "rejected": "WARNING"}.get(result.status, "DEFAULT")
            message = result.failure_reason or (result.blocked or {}).get("reason") or (
                f"status {result.status}" if level != "DEFAULT" else None)
            self._root.update(
                output=[{"role": "assistant", "content": result.answer}],
                metadata={"status": result.status, "tools_executed": result.tools_executed,
                          "hitl_decision": result.hitl_decision, "estimated_cost_usd": result.estimated_cost_usd},
                level=level, status_message=message,
            )
        except Exception as exc:
            self._warn("end of run", exc)


@dataclass
class LangfuseSession:
    """The tracer to pass into a run, plus the client to flush when the process is done."""

    tracer: LangfuseTracer
    client: Any

    @property
    def callbacks(self) -> list[Any]:
        """A bare LangChain handler, for callers that only want the integration's spans."""
        return self.tracer.callbacks()

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
        from langfuse.langchain import CallbackHandler  # noqa: F401  (checked here so the error is early)
    except ImportError as exc:
        raise ConfigError("Langfuse is not installed. Run: uv sync --extra observability") from exc

    from salesops_agent.data import RestrictedData
    from salesops_agent.guardrails import SensitiveIndex, load_policy
    from salesops_agent.versioning import git_commit

    public = resolve_secret(settings.root, "LANGFUSE_PUBLIC_KEY", HINT).value
    secret = resolve_secret(settings.root, "LANGFUSE_SECRET_KEY", HINT).value
    host = resolve_secret(settings.root, "LANGFUSE_HOST", HINT).value
    from salesops_agent.prompts import load_prompt

    policy = load_policy(settings.root / "config" / "policy.yaml")
    redactor = Redactor(policy, SensitiveIndex.build(RestrictedData.load(settings.path("data_dir"))))
    system_prompt = load_prompt(settings).text
    environment = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT") or "development"
    client = Langfuse(public_key=public, secret_key=secret, host=host, mask=make_mask(redactor, frozenset({system_prompt})),
                      environment=environment, release=git_commit(settings.root) or "unversioned", **client_kwargs)
    return LangfuseSession(tracer=LangfuseTracer(client=client, public_key=public), client=client)
