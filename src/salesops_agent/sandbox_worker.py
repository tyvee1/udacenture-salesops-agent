"""Child-process half of the sandbox. Standard library only; started as a script, never imported.

The parent (:mod:`salesops_agent.sandbox`) starts this file with
``python -I sandbox_worker.py`` and an empty environment, then writes one JSON
object to its stdin::

    {"code": "...", "data": {"accounts": [...], ...}, "builtins": [...],
     "limits": {"cpu_seconds": 5, "memory_mb": 512, "max_output_chars": 20000}}

The worker applies resource limits to itself, executes the already-validated
code with only the allowed builtins and the approved datasets in scope, and
writes one JSON object to stdout: ``{"status": "ok", "result": ...}`` or
``{"status": "error", "error": "..."}``.

``-I`` (isolated mode) means the script's own directory, the current directory,
PYTHONPATH and user site-packages are not on the import path, so a planted
module cannot be picked up by the interpreter's own imports.
"""

import builtins
import json
import sys


def _apply_limits(limits):
    """Best-effort OS limits. Each is attempted separately; a platform that refuses one keeps the rest."""
    try:
        import resource
    except ImportError:  # pragma: no cover - not available on Windows
        return
    cpu = int(limits.get("cpu_seconds", 5))
    mem = int(limits.get("memory_mb", 512)) * 1024 * 1024
    for name, value in (("RLIMIT_CPU", (cpu, cpu)), ("RLIMIT_AS", (mem, mem)),
                        ("RLIMIT_FSIZE", (0, 0)), ("RLIMIT_NPROC", (0, 0))):
        res = getattr(resource, name, None)
        if res is None:
            continue
        try:
            resource.setrlimit(res, value)
        except (ValueError, OSError):
            pass


def main():
    request = json.loads(sys.stdin.read())
    _apply_limits(request.get("limits", {}))
    safe_builtins = {name: getattr(builtins, name) for name in request["builtins"] if hasattr(builtins, name)}
    scope = {"__builtins__": safe_builtins}
    scope.update(request["data"])
    max_chars = int(request.get("limits", {}).get("max_output_chars", 20000))
    try:
        exec(compile(request["code"], "<analysis>", "exec"), scope)  # noqa: S102 - validated, restricted scope
        if "result" not in scope:
            out = {"status": "error", "error": "code must assign its answer to a variable named result"}
        else:
            text = json.dumps(scope["result"], default=str)
            if len(text) > max_chars:
                out = {"status": "error", "error": f"result is {len(text)} characters; limit is {max_chars}. Aggregate further."}
            else:
                out = {"status": "ok", "result": json.loads(text)}
    except MemoryError:
        out = {"status": "error", "error": "memory limit exceeded"}
    except Exception as exc:  # report the error type and message, not a traceback with paths
        out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
