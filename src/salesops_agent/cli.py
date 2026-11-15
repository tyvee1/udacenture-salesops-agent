"""Command-line entry point: ``uv run salesops-agent <command>``.

Commands
--------
``ask "question"``            ask the agent one question (the graded entry point)
``history``                    list recent runs from the run-history store
``replay RUN_ID [--rerun]``    inspect a past run, show what changed since, optionally re-run it
``isolation-demo``             key-free demonstration that one user's memory never reaches another
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from salesops_agent.config import ConfigError, load_settings
from salesops_agent.state import InvalidIdentifier


def build_parser() -> argparse.ArgumentParser:
    """Define the ``salesops-agent`` command and its subcommands."""
    parser = argparse.ArgumentParser(prog="salesops-agent", description="UdaCenture SalesOps agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="Ask the agent one question")
    p_ask.add_argument("question", help="The SalesOps question, in quotes")
    p_ask.add_argument(
        "--approval", choices=["approve", "reject"],
        help="Decision for any high-risk action (sending email). Without it, an interactive terminal "
             "is asked; a non-interactive run rejects.",
    )
    _identity_args(p_ask)
    p_ask.add_argument("--langfuse", action="store_true",
                       help="Also send this run to Langfuse (needs the observability extra and LANGFUSE_* keys)")

    p_hist = sub.add_parser("history", help="List recent runs")
    p_hist.add_argument("--user-id", help="Only this user's runs")
    p_hist.add_argument("--limit", type=int, default=20)

    p_replay = sub.add_parser("replay", help="Inspect a past run and optionally re-run it")
    p_replay.add_argument("run_id")
    p_replay.add_argument("--rerun", action="store_true", help="Ask the same question again and compare")
    p_replay.add_argument("--approval", choices=["approve", "reject"],
                          help="Approval decision for the re-run (defaults to the original decision)")

    sub.add_parser("isolation-demo", help="Show, without a model key, that users' memories are isolated")
    return parser


def _identity_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user-id", help="Who is asking (default: anonymous)")
    p.add_argument("--session-id", help="Session id (default: a new session per run)")
    p.add_argument("--thread-id", help="Conversation thread; reuse it to continue a conversation")


def prompt_for_approval(pending: dict) -> str:
    """Show the paused action and ask the person at the terminal to approve or reject it."""
    args = pending.get("args", {})
    print("\n=== Approval required ===", file=sys.stderr)
    print(f"Action: {pending.get('tool')}", file=sys.stderr)
    for key in ("to", "subject", "body"):
        if key in args:
            print(f"{key.capitalize()}: {args[key]}", file=sys.stderr)
    reply = input("Approve this action? [y/N] ").strip().lower()
    return "approve" if reply in {"y", "yes"} else "reject"


def _print_result(result: Any) -> int:
    print(result.answer)
    for a in result.approvals:
        print(f"[approval] {a['tool']}: {a['decision']} (source: {a['source']})")
    if result.blocked:
        print(json.dumps(result.blocked))
    print(f"[run {result.run_id}] status={result.status} user={result.identity.get('user_id')} "
          f"thread={result.identity.get('thread_id')} trace={result.trace_path} log=logs/runs.jsonl", file=sys.stderr)
    if result.status == "error":
        print(f"error: {result.failure_reason}", file=sys.stderr)
        return 2 if (result.failure_reason or "").startswith("configuration error") else 1
    return 3 if result.status == "blocked" else 0


def cmd_ask(args: argparse.Namespace) -> int:
    from salesops_agent.service import ask

    session = None
    if args.langfuse:
        from salesops_agent.external_tracing import langfuse_session

        session = langfuse_session(load_settings())
    approver = prompt_for_approval if args.approval is None and sys.stdin.isatty() else None
    result = ask(args.question, approval=args.approval, approver=approver, user_id=args.user_id,
                 session_id=args.session_id, thread_id=args.thread_id,
                 tracer=session.tracer if session else None)
    if session:
        session.flush()
        # flush() returns once the client has finished trying; it does not report whether the
        # server accepted the spans. Say only what is known, and where to confirm delivery.
        print(f"[langfuse] trace for run {result.run_id} flushed to {session.client_host} (redacted). "
              "Confirm it under Tracing > Traces; a 'Failed to export spans' line above means it did not arrive.",
              file=sys.stderr)
    return _print_result(result)


def cmd_history(args: argparse.Namespace) -> int:
    from salesops_agent.state import StateStore

    store = StateStore(load_settings().path("state_dir"))
    rows = store.history(args.user_id, args.limit)
    store.close()
    if not rows:
        print("No runs recorded yet.")
        return 0
    print(f"{'run_id':<13} {'timestamp':<26} {'user':<12} {'thread':<16} {'status':<10} input")
    for r in rows:
        print(f"{r['run_id']:<13} {r['timestamp']:<26} {r['user_id'][:12]:<12} {r['thread_id'][:16]:<16} "
              f"{r['final_status']:<10} {r['user_input'][:60]}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from salesops_agent.prompts import load_prompt
    from salesops_agent.replay import compare_outcomes, load_trace, outcome, version_changes
    from salesops_agent.state import StateStore
    from salesops_agent.versioning import component_versions

    settings = load_settings()
    store = StateStore(settings.path("state_dir"))
    row = store.get_run(args.run_id)
    store.close()
    if row is None:
        print(f"No run {args.run_id} in the history store.", file=sys.stderr)
        return 1
    old_trace = load_trace(settings.root, row["trace_file"])
    recorded = json.loads(row["versions"])
    current = component_versions(settings, load_prompt(settings))
    print(f"Run {row['run_id']} at {row['timestamp']} by {row['user_id']} (thread {row['thread_id']})")
    print(f"  input:   {row['user_input']}")
    print(f"  status:  {row['final_status']}   approval: {row['hitl_decision']}   model: {row['model']}")
    if old_trace:
        print(f"  outcome: {json.dumps(outcome(old_trace))}")
    changes = version_changes(recorded, current)
    print("  changed since this run: " + (", ".join(changes) if changes else "nothing — same prompt, config, "
                                                                              "tools, policy and dependencies"))
    if not args.rerun:
        return 0

    from salesops_agent.service import ask

    result = ask(row["user_input"], settings, approval=args.approval or row["hitl_decision"],
                 user_id=row["user_id"])
    new_trace = load_trace(settings.root, result.trace_path) if result.trace_path else None
    print(f"\nRe-run {result.run_id}: status {result.status}")
    if old_trace and new_trace:
        diffs = compare_outcomes(outcome(old_trace), outcome(new_trace))
        print("  outcome differences: " + ("; ".join(diffs) if diffs else "none"))
    return 0


def cmd_isolation_demo(_args: argparse.Namespace) -> int:
    """Two users, the same thread name, a scripted model that reports what it was shown."""
    import uuid

    from salesops_agent.scripted_model import ScriptedChatModel, answer
    from salesops_agent.service import ask

    settings = load_settings()
    thread = f"demo-{uuid.uuid4().hex[:6]}"

    def recall(messages):
        said = [m.content for m in messages if m.type == "human"]
        focus = next((s for s in said if "focus" in s.lower() and "?" not in s), None)
        return answer(f"I can see {len(said)} message(s) from you in this conversation. "
                      + (f"Earlier you said: \"{focus}\"" if focus else "You have not told me a focus account."))

    def model():
        return ScriptedChatModel(model_name="isolation-demo", turns=[recall])

    steps = [
        ("demo-alice", "My focus account this week is TechStart Inc."),
        ("demo-bob", "Which account did I say was my focus?"),
        ("demo-alice", "Which account did I say was my focus?"),
    ]
    print(f"Scripted demonstration (no model key needed). Both users use thread id '{thread}'.\n")
    for user, question in steps:
        r = ask(question, settings, model=model(), user_id=user, session_id="demo", thread_id=thread)
        print(f"{user:>11} asks: {question}\n{'':>11} agent: {r.answer}\n")
    print("demo-bob shares alice's thread id but not her conversation: the memory key includes the user id.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the process exit code.

    ``ask`` exit codes: 0 answered (including a rejected approval, which is a normal
    outcome), 3 blocked by a guardrail, 1 run error, 2 configuration error.
    """
    args = build_parser().parse_args(argv)
    handlers = {"ask": cmd_ask, "history": cmd_history, "replay": cmd_replay, "isolation-demo": cmd_isolation_demo}
    try:
        return handlers[args.command](args)
    except (ConfigError, InvalidIdentifier) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
