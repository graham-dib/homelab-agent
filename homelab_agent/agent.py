"""Minimal single-agent ReAct loop over the system tools.

Run with: python -m homelab_agent.agent
"""
from __future__ import annotations

import os
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_agent

from homelab_agent.tools.system_tools import SYSTEM_TOOLS
from homelab_agent.tools.docker_tools import DOCKER_TOOLS

ALL_TOOLS = SYSTEM_TOOLS + DOCKER_TOOLS


load_dotenv()

SYSTEM_PROMPT = """You are an operator's assistant for a Linux home server called \
dibo, running Ubuntu. dibo hosts Plex Media Server, AdGuard Home (DNS filtering), \
Transmission (torrents), and the TP-Link Omada wireless controller, all in Docker.

When the user asks a question:
- Use the available tools to gather evidence before answering.
- Be specific with numbers and units (GB, MB, percentages).
- For diagnostic questions ("why is X slow"), check multiple relevant signals \
(disk, memory, load, service status) and synthesise.
- If a tool returns surprising values, mention them explicitly.
- If you don't have a tool for something, say so rather than guessing.
"""


def build_agent():
    """Construct the ReAct agent. Returns a runnable graph."""
    model = ChatAnthropic(
        model="claude-sonnet-4-5-20250929",
        temperature=0,
    )
    return create_agent(
        model=model,
        tools=ALL_TOOLS,
        prompt=SYSTEM_PROMPT,
    )


def run_query(question: str) -> None:
    """Run a single question through the agent and print the trace."""
    agent = build_agent()

    print(f"\n{'=' * 70}")
    print(f"QUESTION: {question}")
    print(f"{'=' * 70}\n")

    result = agent.invoke({"messages": [("user", question)]})

    for msg in result["messages"]:
        msg_type = msg.type
        if msg_type == "human":
            continue  # already printed above
        if msg_type == "ai":
            # AI message - either tool calls or final answer
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"[TOOL CALL] {tc['name']}({tc['args']})")
            if msg.content:
                # content can be a string OR a list of content blocks
                if isinstance(msg.content, str):
                    print(f"\n[ASSISTANT]\n{msg.content}\n")
                else:
                    for block in msg.content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            print(f"\n[ASSISTANT]\n{block['text']}\n")
        elif msg_type == "tool":
            # Truncate noisy tool output for readability
            content = str(msg.content)
            if len(content) > 400:
                content = content[:400] + "... [truncated]"
            print(f"[TOOL RESULT] {content}\n")


if __name__ == "__main__":
    questions = [
        "How is dibo doing right now? Give me a one-paragraph health summary.",
        "Is the disk getting full anywhere?",
        "What's using the most memory on dibo, and is that concerning?",
        "Are all my containers healthy?",
        "What's the most resource-hungry container right now?",
        "Has anything restarted recently?",
        "Show me the last 20 lines of the Plex container logs.",
    ]
    for q in questions:
        run_query(q)