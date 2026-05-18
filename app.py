"""Streamlit chat UI for dibo-agent.

Launch with:  streamlit run app.py
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import streamlit as st
from dotenv import load_dotenv
from langgraph.types import Command

load_dotenv()

st.set_page_config(
    page_title="dibo-agent",
    page_icon="🖥️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from homelab_agent.multi_agent import build_supervisor, _log_action_proposal
from homelab_agent.cost_tracking import UsageTracker
from homelab_agent.ingest.schema import get_connection
from homelab_agent.tools.system_tools import check_dibo_reachable


# ── Cached resources ──────────────────────────────────────────────────────────

@st.cache_resource(show_spinner="Building agent…")
def get_supervisor():
    return build_supervisor()


@st.cache_data(ttl=30)
def get_dibo_status() -> dict:
    try:
        return check_dibo_reachable.invoke({})
    except Exception as e:
        return {"reachable": False, "error": str(e)}


def get_total_cost() -> float:
    try:
        conn = get_connection()
        result = conn.execute(
            "SELECT SUM(estimated_cost_usd) FROM agent_usage"
        ).fetchone()
        conn.close()
        return result[0] or 0.0
    except Exception:
        return 0.0


# ── Session state ─────────────────────────────────────────────────────────────

def _init_state() -> None:
    defaults: dict = {
        "messages": [],   # {role, content, trace, cost_usd}
        "pending": None,  # {interrupt_value, config, question} | None
        "tracker": None,  # UsageTracker kept alive across HITL resume
        "session_cost": 0.0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Stream collection ─────────────────────────────────────────────────────────

def _collect_stream(gen) -> tuple[str, list, bool, dict | None]:
    """Drain a LangGraph stream generator into structured output.

    Returns (final_answer, trace_events, interrupted, interrupt_value).
    """
    trace: list[dict] = []
    answer = ""
    interrupted = False
    interrupt_value = None

    for chunk in gen:
        if "__interrupt__" in chunk:
            items = chunk["__interrupt__"]
            if items:
                interrupt_value = items[0].value
            interrupted = True
            break

        for node_name, node_output in chunk.items():
            if not isinstance(node_output, dict):
                continue
            for msg in node_output.get("messages", []):
                mtype = getattr(msg, "type", None)
                if mtype == "ai":
                    agent_name = getattr(msg, "name", None) or node_name
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            trace.append({
                                "type": "call",
                                "agent": agent_name,
                                "tool": tc["name"],
                                "args": tc.get("args", {}),
                            })
                    content = msg.content
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if content and not msg.tool_calls:
                        answer = content
                elif mtype == "tool":
                    raw = str(msg.content)
                    trace.append({
                        "type": "result",
                        "tool": getattr(msg, "name", "tool"),
                        "content": raw[:800] + ("…" if len(raw) > 800 else ""),
                    })

    return answer, trace, interrupted, interrupt_value


# ── Sidebar ───────────────────────────────────────────────────────────────────

def _render_sidebar() -> None:
    with st.sidebar:
        st.title("dibo-agent")
        st.caption("Multi-agent homelab monitor")

        status = get_dibo_status()
        if status.get("reachable"):
            st.success(f"● dibo online", icon=None)
        else:
            st.error("● dibo unreachable")

        st.divider()

        col_a, col_b = st.columns(2)
        col_a.metric("Session", f"${st.session_state.session_cost:.4f}")
        col_b.metric("All-time", f"${get_total_cost():.4f}")

        st.divider()

        if st.button("New conversation", use_container_width=True):
            st.session_state.messages = []
            st.session_state.pending = None
            st.session_state.tracker = None
            st.session_state.session_cost = 0.0
            st.rerun()

        st.divider()
        st.caption("Subagents: Infra · Network · Media")
        st.caption("Write tools: restart_container · flush_adguard_cache · reboot_dibo")
        st.caption("Model: Claude Sonnet 4.5")


# ── Trace panel ───────────────────────────────────────────────────────────────

def _render_trace(trace: list[dict], cost_usd: float) -> None:
    if not trace:
        return
    calls = sum(1 for e in trace if e["type"] == "call")
    label = f"↳ {calls} tool call{'s' if calls != 1 else ''} · ${cost_usd:.4f}"
    with st.expander(label, expanded=False):
        for event in trace:
            if event["type"] == "call":
                agent = event["agent"]
                tool = event["tool"]
                args = event.get("args", {})
                st.markdown(f"**`{agent}`** → `{tool}`")
                if args:
                    st.code(json.dumps(args, indent=2), language="json")
            elif event["type"] == "result":
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ `{event['tool']}`")
                st.code(event["content"], language="text")
            st.divider()


# ── HITL banner ───────────────────────────────────────────────────────────────

def _render_hitl_banner() -> str | None:
    """Show the approval widget. Returns 'approved', 'rejected', or None."""
    pending = st.session_state.get("pending")
    if not pending:
        return None

    iv = pending["interrupt_value"]
    action = iv.get("action", "unknown")
    args = iv.get("args", {})
    warning = iv.get("warning", "")
    reversible = iv.get("reversible", True)

    st.warning(
        f"**Action requires approval** · `{action}`"
        + (f"\n\nArgs: `{json.dumps(args)}`" if args else ""),
        icon="⚠️",
    )
    if warning:
        st.caption(warning)
    if not reversible:
        st.error("This action cannot be undone.", icon="🔴")

    col_approve, col_reject, _ = st.columns([1, 1, 5])
    approve_clicked = col_approve.button(
        "Approve", type="primary", use_container_width=True, key="hitl_approve"
    )
    reject_clicked = col_reject.button(
        "Reject", use_container_width=True, key="hitl_reject"
    )

    if approve_clicked:
        return "approved"
    if reject_clicked:
        return "rejected"
    return None


# ── HITL resume handler ───────────────────────────────────────────────────────

def _handle_hitl_decision(decision: str) -> None:
    pending = st.session_state.pending
    supervisor = get_supervisor()
    tracker = st.session_state.tracker
    config = pending["config"]
    question = pending["question"]
    interrupt_value = pending["interrupt_value"]

    _log_action_proposal(question, interrupt_value, decision, datetime.now(timezone.utc))

    invoke_config = {**config, "callbacks": [tracker]}

    with st.spinner("Resuming…"):
        gen = supervisor.stream(
            Command(resume=decision),
            config=invoke_config,
            stream_mode="updates",
        )
        answer, trace, interrupted, next_interrupt = _collect_stream(gen)

    if not interrupted:
        usage = tracker.persist()
        cost = usage.get("estimated_cost_usd", 0.0)
        st.session_state.session_cost += cost
        st.session_state.tracker = None
        st.session_state.pending = None
    else:
        # Another approval gate
        cost = 0.0
        st.session_state.pending = {
            "interrupt_value": next_interrupt,
            "config": config,
            "question": question,
        }

    action_label = "✓ Approved" if decision == "approved" else "✗ Rejected"
    display = answer or f"[{action_label}] `{interrupt_value.get('action', 'action')}` — no further output."

    st.session_state.messages.append({
        "role": "assistant",
        "content": display,
        "trace": trace,
        "cost_usd": cost,
        "hitl_decision": decision,
    })
    st.rerun()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _init_state()
    _render_sidebar()

    st.header("dibo-agent", divider="gray")

    # HITL approval banner (above chat history)
    decision = _render_hitl_banner()
    if decision:
        _handle_hitl_decision(decision)
        return  # _handle_hitl_decision calls st.rerun()

    # Chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant":
                _render_trace(msg.get("trace", []), msg.get("cost_usd", 0.0))

    # Disable input while HITL is pending
    is_pending = st.session_state.pending is not None
    if is_pending:
        st.info("Waiting for your approval above before accepting new queries.", icon="ℹ️")

    # Chat input
    prompt = st.chat_input(
        "Ask about dibo…",
        disabled=is_pending,
    )
    if not prompt:
        return

    # Show user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Build thread and tracker
    supervisor = get_supervisor()
    config = {"configurable": {"thread_id": str(uuid4())}}
    tracker = UsageTracker(prompt)

    invoke_config = {**config, "callbacks": [tracker]}

    # Stream the response
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            gen = supervisor.stream(
                {"messages": [("user", prompt)]},
                config=invoke_config,
                stream_mode="updates",
            )
            answer, trace, interrupted, interrupt_value = _collect_stream(gen)

    if interrupted and interrupt_value:
        # Persist pre-interrupt usage as a partial record if there were LLM calls
        st.session_state.tracker = tracker
        st.session_state.pending = {
            "interrupt_value": interrupt_value,
            "config": config,
            "question": prompt,
        }
        if answer:
            st.session_state.messages.append({
                "role": "assistant",
                "content": answer,
                "trace": trace,
                "cost_usd": 0.0,
            })
        st.rerun()
    else:
        usage = tracker.persist()
        cost = usage.get("estimated_cost_usd", 0.0)
        st.session_state.session_cost += cost
        st.session_state.messages.append({
            "role": "assistant",
            "content": answer or "_(no response)_",
            "trace": trace,
            "cost_usd": cost,
        })
        st.rerun()


if __name__ == "__main__":
    main()
