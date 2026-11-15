"""Streamlit front end for the SalesOps agent, including the approval interface.

Start it with ``uv run --extra ui salesops-ui``. It drives the same service as
the CLI, so every guardrail, the approval gate, logging and tracing apply
unchanged. The differences:

* When the agent pauses before a high-risk action, the page shows the exact
  pending email with **Approve** and **Reject** buttons, then resumes the same
  run with the decision (recorded with source ``ui``).
* The sidebar sets the user, session and thread ids, so per-user memory
  isolation can be tried by switching users.
* Tabs show this user's run history and the monitoring report.

``st.session_state["model_override"]``, if set, replaces the configured model.
It exists so the automated UI test can drive the page with a scripted model; the
app never sets it itself.
"""

from __future__ import annotations

import json

import streamlit as st

from salesops_agent.config import ConfigError, load_settings, resolve_api_key
from salesops_agent.monitoring import aggregate, render_markdown
from salesops_agent.observability import read_log
from salesops_agent.service import resume, start_run
from salesops_agent.state import InvalidIdentifier, StateStore

st.set_page_config(page_title="UdaCenture SalesOps Agent", layout="wide")
settings = load_settings()

# ------------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Who is asking")
    user_id = st.text_input("User id", value="ui-user")
    session_id = st.text_input("Session id", value="ui-session")
    thread_id = st.text_input("Thread id", value="main", help="Reuse a thread id to continue a conversation.")
    st.divider()
    try:
        key = resolve_api_key(settings)
        st.success(f"Model key found in {key.source} ({key.fingerprint})")
    except ConfigError as exc:
        if st.session_state.get("model_override") is None:
            st.warning(str(exc))
    st.caption(f"Model: {settings.model.provider}:{settings.model.name} · prompt v{settings.prompt_version}")

identity = (user_id, session_id, thread_id)
transcript_key = f"transcript::{'::'.join(identity)}"
st.session_state.setdefault(transcript_key, [])
st.session_state.setdefault("pending_run", None)

st.title("UdaCenture SalesOps Agent")
tab_chat, tab_history, tab_monitor = st.tabs(["Ask", "Run history", "Monitoring"])


def _record(result) -> None:
    """Add a finished result to this identity's on-screen transcript."""
    st.session_state[transcript_key].append({
        "role": "assistant", "content": result.answer, "status": result.status, "run_id": result.run_id,
        "blocked": result.blocked, "approvals": result.approvals, "trace": result.trace_path,
    })


def _decide(decision: str) -> None:
    run = st.session_state["pending_run"]
    st.session_state["pending_run"] = None
    _record(resume(run, decision))


# ---------------------------------------------------------------------------- ask
with tab_chat:
    for msg in st.session_state[transcript_key]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"] or "_(no answer)_")
            if msg["role"] == "assistant":
                st.caption(f"run {msg['run_id']} · status **{msg['status']}** · trace `{msg['trace']}`")
                if msg.get("blocked"):
                    st.code(json.dumps(msg["blocked"], indent=2), language="json")
                for a in msg.get("approvals", []):
                    st.caption(f"approval for {a['tool']}: **{a['decision']}** ({a['source']})")

    pending = st.session_state["pending_run"]
    if pending is not None:
        st.warning("Approval required before this action can run.")
        for item in pending.pending:
            args = item.value.get("args", {})
            st.markdown(f"**Action:** `{item.value.get('tool')}`")
            st.text_input("To", args.get("to", ""), disabled=True, key=f"to-{item.id}")
            st.text_input("Subject", args.get("subject", ""), disabled=True, key=f"subj-{item.id}")
            st.text_area("Body", args.get("body", ""), disabled=True, key=f"body-{item.id}")
        c1, c2 = st.columns(2)
        c1.button("Approve", type="primary", on_click=_decide, args=("approve",), key="approve-btn")
        c2.button("Reject", on_click=_decide, args=("reject",), key="reject-btn")

    question = st.chat_input("Ask about accounts, pipeline, renewals, or ask for a follow-up email",
                             disabled=pending is not None)
    if question:
        st.session_state[transcript_key].append({"role": "user", "content": question})
        try:
            run, result = start_run(question, settings, model=st.session_state.get("model_override"),
                                    defer_approval=True, user_id=user_id, session_id=session_id, thread_id=thread_id)
        except InvalidIdentifier as exc:
            st.error(str(exc))
        else:
            if result.status == "awaiting_approval":
                st.session_state["pending_run"] = run
            else:
                _record(result)
        st.rerun()

# ------------------------------------------------------------------------ history
with tab_history:
    try:
        store = StateStore(settings.path("state_dir"))
        rows = store.history(user_id, limit=50)
        store.close()
        st.caption(f"Runs recorded for user '{user_id}' (other users' runs are not shown).")
        st.dataframe([{k: r[k] for k in ("run_id", "timestamp", "thread_id", "final_status", "hitl_decision",
                                         "user_input")} for r in rows], use_container_width=True)
    except InvalidIdentifier as exc:
        st.error(str(exc))

# --------------------------------------------------------------------- monitoring
with tab_monitor:
    metrics = aggregate(read_log(settings.path("logs_dir")))
    st.markdown(render_markdown(metrics, "live, from logs/runs.jsonl"))
