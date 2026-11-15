"""Sandboxed analysis over approved CRM data: static validation, then a restricted child process.

Two layers, so neither has to be perfect alone:

1. :func:`validate_code` parses the submitted code and walks its syntax tree.
   Only node types, names and attribute names on the allowlists in
   ``config/sandbox.yaml`` pass. Imports, function and class definitions,
   ``with``/``try``, names or attributes starting with an underscore, and the
   introspection builtins (``eval``, ``exec``, ``open``, ``getattr``,
   ``__import__``…) are refused *before anything runs*. The tool guardrail calls
   this, so a violation is a logged policy block.
2. :func:`run_analysis` executes validated code in a separate interpreter
   (``python -I``) with an empty environment — so no API key is visible to it —
   CPU, memory and file-size limits, and a wall-clock timeout enforced by the
   parent. Only the approved datasets are serialized into it; the HR and
   financial files are never loaded.

Python-level restrictions are not a security boundary against a determined
attacker on their own. That is why the code also runs out of process with OS
limits, and why the README lists container/VM isolation as production hardening.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Names that are refused even if someone adds them to the builtins allowlist by mistake.
ALWAYS_DENIED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "help", "exit", "quit", "memoryview", "type", "object", "super",
    "classmethod", "staticmethod", "property", "dir", "id",
})

WORKER = Path(__file__).with_name("sandbox_worker.py")


class SandboxViolation(ValueError):
    """The code failed static validation and was not executed."""


@dataclass(frozen=True)
class SandboxConfig:
    """Compiled view of ``config/sandbox.yaml``."""

    timeout_seconds: float
    cpu_seconds: int
    memory_mb: int
    max_code_chars: int
    max_output_chars: int
    datasets: tuple[str, ...]
    exclude_fields: dict[str, tuple[str, ...]]
    allowed_builtins: frozenset[str]
    allowed_attributes: frozenset[str]
    allowed_nodes: frozenset[str]

    @classmethod
    def load(cls, path: Path) -> "SandboxConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        lim = raw["limits"]
        return cls(
            timeout_seconds=float(lim["timeout_seconds"]),
            cpu_seconds=int(lim["cpu_seconds"]),
            memory_mb=int(lim["memory_mb"]),
            max_code_chars=int(lim["max_code_chars"]),
            max_output_chars=int(lim["max_output_chars"]),
            datasets=tuple(raw["datasets"]),
            exclude_fields={k: tuple(v) for k, v in (raw.get("exclude_fields") or {}).items()},
            allowed_builtins=frozenset(raw["allowed_builtins"]) - ALWAYS_DENIED_NAMES,
            allowed_attributes=frozenset(raw["allowed_attributes"]),
            allowed_nodes=frozenset(raw["allowed_nodes"]),
        )


#: A block statement joined to other code on one line: ``x = 1; for ...`` or ``for ...: if ...:``.
_JOINED_BLOCK = re.compile(r";\s*(for|if|elif|else|while|with|try)\b|:\s*(for|if|while|with|try)\b[^\n]*:")

ONE_STATEMENT_PER_LINE = (
    "Python does not allow a for/if/while block after ';' or on the same line as another block. "
    "Put each statement on its own line, using real line breaks, and indent the body of each "
    "block by 4 spaces. For example:\n"
    "counts = {}\n"
    "for t in support_tickets:\n"
    "    if t['status'] != 'resolved':\n"
    "        counts[t['severity']] = counts.get(t['severity'], 0) + 1\n"
    "result = counts"
)


def syntax_error_detail(code: str, exc: SyntaxError) -> str:
    """A refusal message the model can act on, not just "invalid syntax (line 1)".

    Why: in a live run the model wrote its analysis on one line, joining a ``for``
    loop and an ``if`` with semicolons. The bare message told it nothing, so it
    resubmitted the same shape twice and then abandoned the tool it was asked to
    use. The message now quotes the offending line and, for that common mistake,
    says exactly how to fix it.
    """
    line = (exc.text or "").strip()
    if len(line) > 160:
        line = line[:157] + "..."
    detail = f"syntax error: {exc.msg} (line {exc.lineno})"
    if line:
        detail += f": {line}"
    if _JOINED_BLOCK.search(code):
        detail += "\n" + ONE_STATEMENT_PER_LINE
    return detail


def validate_code(code: str, cfg: SandboxConfig) -> None:
    """Raise :class:`SandboxViolation` unless ``code`` uses only allowed syntax, names and attributes.

    Names are allowed if they are an allowed builtin, an approved dataset, or a
    variable the code itself assigns (including loop and comprehension targets
    and lambda parameters). Everything else — including any name or attribute
    beginning with ``_`` — is refused.
    """
    if not isinstance(code, str) or not code.strip():
        raise SandboxViolation("no code submitted")
    if len(code) > cfg.max_code_chars:
        raise SandboxViolation(f"code is {len(code)} characters; limit is {cfg.max_code_chars}")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxViolation(syntax_error_detail(code, exc)) from exc

    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned.add(node.id)
        elif isinstance(node, ast.arg):
            assigned.add(node.arg)
    allowed_names = cfg.allowed_builtins | set(cfg.datasets) | assigned

    for node in ast.walk(tree):
        kind = type(node).__name__
        if kind not in cfg.allowed_nodes:
            raise SandboxViolation(f"'{kind}' is not allowed (line {getattr(node, 'lineno', '?')})")
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id in ALWAYS_DENIED_NAMES:
                raise SandboxViolation(f"name '{node.id}' is not allowed")
            if isinstance(node.ctx, ast.Load) and node.id not in allowed_names:
                raise SandboxViolation(f"name '{node.id}' is not available in the sandbox")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr not in cfg.allowed_attributes:
                raise SandboxViolation(f"attribute '.{node.attr}' is not allowed")
        elif isinstance(node, ast.arg) and node.arg.startswith("_"):
            raise SandboxViolation(f"parameter '{node.arg}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            raise SandboxViolation("bytes literals are not allowed")


def approved_data(crm: Any, cfg: SandboxConfig) -> dict[str, list[dict[str, Any]]]:
    """Copy the approved datasets, dropping excluded fields. Restricted data is never touched."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name in cfg.datasets:
        drop = set(cfg.exclude_fields.get(name, ()))
        out[name] = [{k: v for k, v in row.items() if k not in drop} for row in getattr(crm, name)]
    return out


def run_analysis(code: str, crm: Any, cfg: SandboxConfig) -> dict[str, Any]:
    """Validate and execute ``code``; return ``{"status": "ok", "result": ...}`` or an error dict."""
    try:
        validate_code(code, cfg)
    except SandboxViolation as exc:
        return {"status": "blocked", "reason": "sandbox_violation", "detail": str(exc)}

    request = {
        "code": code,
        "data": approved_data(crm, cfg),
        "builtins": sorted(cfg.allowed_builtins),
        "limits": {"cpu_seconds": cfg.cpu_seconds, "memory_mb": cfg.memory_mb,
                   "max_output_chars": cfg.max_output_chars},
    }
    return spawn_worker(request, cfg.timeout_seconds)


def spawn_worker(request: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    """Run the worker process on a prepared request. Separate so tests can probe the second layer alone."""
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(WORKER)],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=timeout_seconds, env={}, cwd=str(WORKER.parent), check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"timed out after {timeout_seconds:g} seconds"}
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = "killed by a resource limit" if proc.returncode < 0 else f"exit code {proc.returncode}"
        return {"status": "error", "error": f"analysis process failed ({detail})"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": "analysis process returned malformed output"}
