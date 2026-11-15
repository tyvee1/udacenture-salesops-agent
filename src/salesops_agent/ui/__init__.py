"""Streamlit front end. ``launch`` is the ``salesops-ui`` console script."""

from __future__ import annotations

import sys
from pathlib import Path


def launch() -> int:
    """Start the Streamlit app from the project root (``uv run --extra ui salesops-ui``)."""
    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print("Streamlit is not installed. Run: uv sync --extra ui", file=sys.stderr)
        return 2
    app = Path(__file__).with_name("app.py")
    sys.argv = ["streamlit", "run", str(app), *sys.argv[1:]]
    return stcli.main()
