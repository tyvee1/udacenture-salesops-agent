"""Command-line entry point: ``uv run salesops-agent ask "question"``."""

from __future__ import annotations

import argparse
import json
import sys

from salesops_agent.config import ConfigError
from salesops_agent.service import ask


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
    return parser


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


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the command, print the answer. Returns the process exit code.

    Exit codes: 0 answered (including a rejected approval, which is a normal outcome),
    3 blocked by a guardrail, 1 run error, 2 configuration error.
    """
    args = build_parser().parse_args(argv)
    try:
        approver = prompt_for_approval if args.approval is None and sys.stdin.isatty() else None
        result = ask(args.question, approval=args.approval, approver=approver)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(result.answer)
    for a in result.approvals:
        print(f"[approval] {a['tool']}: {a['decision']} (source: {a['source']})")
    if result.blocked:
        print(json.dumps(result.blocked))
    print(f"[run {result.run_id}] status={result.status} trace={result.trace_path} log=logs/runs.jsonl",
          file=sys.stderr)
    if result.status == "error":
        print(f"error: {result.failure_reason}", file=sys.stderr)
        return 2 if (result.failure_reason or "").startswith("configuration error") else 1
    return 3 if result.status == "blocked" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
