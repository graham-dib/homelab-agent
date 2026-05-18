"""Minimal single-agent ReAct loop over all homelab tools.

Run with: python -m homelab_agent.agent
"""
from __future__ import annotations

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain.agents import create_agent

from homelab_agent.tools.system_tools import SYSTEM_TOOLS
from homelab_agent.tools.docker_tools import DOCKER_TOOLS
from homelab_agent.tools.adguard_tools import ADGUARD_TOOLS
from homelab_agent.tools.history_tools import HISTORY_TOOLS
from homelab_agent.tools.storage_tools import STORAGE_TOOLS
from homelab_agent.cost_tracking import UsageTracker

ALL_TOOLS = SYSTEM_TOOLS + DOCKER_TOOLS + ADGUARD_TOOLS + HISTORY_TOOLS + STORAGE_TOOLS

load_dotenv()

_SYSTEM_PROMPT_TEMPLATE = """You are an operator's assistant for a Linux home server called \
dibo, running Ubuntu. dibo hosts Plex Media Server, AdGuard Home (DNS filtering), \
Transmission (torrents), and the TP-Link Omada wireless controller, all in Docker.

Today's date is {today}.

When the user asks a question:
- Use the available tools to gather evidence before answering.
- Be specific with numbers and units (GB, MB, percentages).
- For diagnostic questions, check multiple relevant signals (disk, memory, load, \
service status) and synthesise.
- For TEMPORAL questions ('is X growing', 'has Y changed', 'what's normal'), first \
call get_snapshot_coverage to find out how much historical data exists. If the data \
span is too short to support the claim being asked about, say so explicitly — don't \
overreach.
- File modification dates returned by tools are accurate — trust them over your \
internal sense of the current date.
- If a tool returns surprising values, mention them explicitly.
- If you don't have a tool for something, say so rather than guessing.

You CAN delete files on dibo. When the user asks you to delete a file, or when \
you are proposing specific files for removal, do the following:
1. Use find_large_files or get_directory_sizes to confirm the exact absolute path \
if you do not already have it.
2. End your response with exactly this block — this is your deletion mechanism:

DELETION_CANDIDATES:
/absolute/path/to/file1.mkv
/absolute/path/to/file2.mkv
END_DELETION_CANDIDATES

The operator will be asked to confirm before anything is removed. Only include \
paths under /srv/. Never say you "don't have a deletion tool" — you do, and \
it is the block above.
"""


def build_agent(llm=None):
    """Construct the ReAct agent. Returns a runnable graph."""
    from datetime import date
    if llm is None:
        llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat())
    return create_agent(
        model=llm,
        tools=ALL_TOOLS,
        system_prompt=system_prompt,
    )


def run_query(question: str) -> None:
    """Run a single question through the agent and print the trace."""
    agent = build_agent()

    print(f"\n{'=' * 70}")
    print(f"QUESTION: {question}")
    print(f"{'=' * 70}\n")

    tracker = UsageTracker(question)
    result = agent.invoke(
        {"messages": [("user", question)]},
        config={"callbacks": [tracker]},
    )

    for msg in result["messages"]:
        msg_type = msg.type
        if msg_type == "human":
            continue
        if msg_type == "ai":
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"[TOOL CALL] {tc['name']}({tc['args']})")
            if msg.content:
                if isinstance(msg.content, str):
                    print(f"\n[ASSISTANT]\n{msg.content}\n")
                else:
                    for block in msg.content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            print(f"\n[ASSISTANT]\n{block['text']}\n")
        elif msg_type == "tool":
            content = str(msg.content)
            if len(content) > 400:
                content = content[:400] + "... [truncated]"
            print(f"[TOOL RESULT] {content}\n")

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
    ]
    for q in questions:
        run_query(q)