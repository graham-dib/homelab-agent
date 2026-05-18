"""Multi-agent entry point: supervisor routing to Infra, Network, and Media subagents,
with HITL approval gates on write actions.

Run with: python -m homelab_agent.multi_agent
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langgraph_supervisor import create_supervisor

from homelab_agent.subagents.infra import build_infra_agent
from homelab_agent.subagents.network import build_network_agent
from homelab_agent.subagents.media import build_media_agent
from homelab_agent.tools.write_tools import WRITE_TOOLS
from homelab_agent.cost_tracking import UsageTracker
from homelab_agent.ingest.schema import get_connection

load_dotenv()

SUPERVISOR_PROMPT = """You are the supervisor for dibo, a Linux home server.

You have three specialist subagents. Route each question to the right one:

- **Infra Subagent**: host-level concerns — disk, memory, CPU load, systemd services,
  journal logs, container fleet (list/stats/restart history), historical system metrics.

- **Network Subagent**: DNS and AdGuard Home — protection status, query counts, blocked
  domains, top clients, historical DNS trends.

- **Media Subagent**: application-level health for Plex, Transmission, and the Omada
  Controller — container logs, health state, restart counts, resource history for those
  specific services.

You also have write tools for taking direct action on dibo. Use them ONLY when the
operator explicitly asks you to act (not just diagnose). Each write tool will pause
and request human approval before executing:

- restart_container(name) — restart a single Docker container
- flush_adguard_cache() — flush AdGuard's DNS response cache
- reboot_dibo() — reboot the entire server (last resort only)

Workflow for write actions:
1. Delegate to the relevant subagent to diagnose and confirm the action is warranted.
2. Call the appropriate write tool — it will interrupt and wait for approval.
3. Report the outcome to the operator.

For cross-cutting questions (e.g. "is dibo healthy?"), delegate to multiple subagents
and synthesise their answers. Always use at least one subagent — never answer from
prior knowledge alone.
"""


def build_supervisor():
    """Construct the supervisor graph with HITL checkpointing. Returns a compiled graph."""
    model = ChatAnthropic(
        model="claude-sonnet-4-5-20250929",
        temperature=0,
    )
    infra = build_infra_agent()
    network = build_network_agent()
    media = build_media_agent()

    return create_supervisor(
        agents=[infra, network, media],
        model=model,
        tools=WRITE_TOOLS,
        prompt=SUPERVISOR_PROMPT,
        output_mode="full_history",
    ).compile(checkpointer=InMemorySaver())


def _print_new_messages(messages: list, seen_ids: set) -> None:
    """Print messages not yet shown. Updates seen_ids in place."""
    for msg in messages:
        msg_id = id(msg)
        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        msg_type = msg.type
        if msg_type == "human":
            continue
        if msg_type == "ai":
            name = getattr(msg, "name", None) or "Supervisor"
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"[{name}] → {tc['name']}({tc['args']})")
            if msg.content:
                content = msg.content
                if isinstance(content, list):
                    content = " ".join(
                        b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if content:
                    print(f"\n[{name}]\n{content}\n")
        elif msg_type == "tool":
            content = str(msg.content)
            if len(content) > 400:
                content = content[:400] + "... [truncated]"
            print(f"[TOOL RESULT] {content}\n")


def _log_action_proposal(
    question: str,
    interrupt_value: dict,
    decision: str,
    decided_at: datetime,
) -> None:
    """Write an action proposal and its decision to the audit log."""
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO action_proposals
               (id, timestamp, question, action, action_args, decision, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                int(time.time() * 1_000_000),
                datetime.now(timezone.utc),
                question[:500],
                interrupt_value.get("action", "unknown"),
                json.dumps(interrupt_value.get("args", {})),
                decision,
                decided_at,
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _handle_approval(interrupt_value: dict) -> str:
    """Show the pending action to the operator and get an approve/reject decision."""
    action = interrupt_value.get("action", "unknown")
    args = interrupt_value.get("args", {})
    warning = interrupt_value.get("warning", "")

    print(f"\n{'!' * 70}")
    print(f"  APPROVAL REQUIRED")
    print(f"  Action : {action}")
    if args:
        print(f"  Args   : {json.dumps(args)}")
    print(f"  Warning: {warning}")
    print(f"{'!' * 70}")

    while True:
        raw = input("  Approve? [y/n]: ").strip().lower()
        if raw in ("y", "yes"):
            return "approved"
        if raw in ("n", "no"):
            return "rejected"
        print("  Please enter 'y' or 'n'.")


def run_query(question: str) -> None:
    """Run a single question through the supervisor, handling HITL interrupts."""
    supervisor = build_supervisor()
    config = {"configurable": {"thread_id": str(uuid4())}}

    print(f"\n{'=' * 70}")
    print(f"QUESTION: {question}")
    print(f"{'=' * 70}\n")

    tracker = UsageTracker(question)
    seen_ids: set = set()
    invoke_config = {**config, "callbacks": [tracker]}

    # First invocation
    result = supervisor.invoke(
        {"messages": [("user", question)]},
        config=invoke_config,
    )
    _print_new_messages(result.get("messages", []), seen_ids)

    # HITL loop — handle any number of consecutive approval gates
    while True:
        state = supervisor.get_state(config)
        if not state.next:
            break

        # Extract interrupt data from the first pending task
        interrupt_value = None
        for task in state.tasks:
            if task.interrupts:
                interrupt_value = task.interrupts[0].value
                break

        if interrupt_value is None:
            break

        decision = _handle_approval(interrupt_value)
        decided_at = datetime.now(timezone.utc)
        _log_action_proposal(question, interrupt_value, decision, decided_at)
        print(f"\n  → Decision: {decision.upper()}\n")

        result = supervisor.invoke(
            Command(resume=decision),
            config=invoke_config,
        )
        _print_new_messages(result.get("messages", []), seen_ids)

    summary = tracker.persist()
    print(
        f"\n[USAGE] {summary['n_llm_calls']} LLM calls · "
        f"{summary['input_tokens']:,} in / {summary['output_tokens']:,} out tokens · "
        f"${summary['estimated_cost_usd']:.4f} · "
        f"{summary['latency_seconds']:.1f}s"
    )


if __name__ == "__main__":
    questions = [
        "How much data have we collected so far? What time range does it cover?",
        "Has /srv/storage usage changed at all recently?",
        "Are AdGuard's DNS query counts trending up or down? Be honest about what the data can support.",
        "Is dibo healthy across the board right now?",
    ]
    for q in questions:
        run_query(q)
