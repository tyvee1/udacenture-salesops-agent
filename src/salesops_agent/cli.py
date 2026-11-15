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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the command, print the answer. Returns the process exit code.

    Exit codes: 0 answered, 3 blocked by a guardrail, 1 run error, 2 configuration error.
    """
    args = build_parser().parse_args(argv)
    try:
        result = ask(args.question)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(result.answer)
    if result.blocked:
        print(json.dumps(result.blocked))
    if result.status == "error":
        print(f"error: {result.failure_reason}", file=sys.stderr)
        return 1
    return 3 if result.status == "blocked" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
