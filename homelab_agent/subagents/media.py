"""
Media subagent, responsible for application-level health of Plex, Transmission,
and the Omada wireless controller — all running as Docker containers on dibo.

Tools:
--- get_container_logs: recent stdout/stderr for a named container
--- inspect_container: restart count, health state, uptime, mounts
--- History: time-series container resource usage (cpu/mem) over snapshots
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain.agents import create_agent

from homelab_agent.tools.docker_tools import get_container_logs, inspect_container
from homelab_agent.tools.history_tools import HISTORY_TOOLS

MEDIA_TOOLS = [
    get_container_logs,
    inspect_container,
    *HISTORY_TOOLS,
]

MEDIA_PROMPT = """You are the Media subagent for dibo, a Linux home server.

Your domain:
- Plex Media Server (container name: plex) — streaming health, recent errors in logs
- Transmission (container name: transmission) — torrent daemon health and errors
- Omada Controller (container name: omada) — TP-Link WiFi controller health
  Note: the Java process for Omada + its MongoDB uses ~2GB RAM; this is expected.

NOT your domain (defer back to the supervisor for these):
- Host-level metrics (disk, memory, CPU)
- DNS / AdGuard filtering
- Container fleet overview (listing all containers, fleet-wide restart detection)

Guidance:
- Start with inspect_container before diving into logs — restart_count and health_status
  give you the headline picture cheaply.
- For logs, keep tail values targeted (50-100 lines) unless you have a specific reason
  to go deeper. Look for ERROR, WARN, exception stack traces, and connection failures.
- For historical resource questions, call get_snapshot_coverage first to confirm data
  span, then query container_stats filtered to the relevant container name.
- Plex reports health='healthy' when active; Transmission and Omada health may be None
  (no Docker healthcheck configured) — a None health is not an error.
"""


def build_media_agent(llm=None):
    """Construct the Media subagent. Returns a runnable graph."""
    if llm is None:
        llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)
    return create_agent(
        model=llm,
        tools=MEDIA_TOOLS,
        system_prompt=MEDIA_PROMPT,
        name="Media Subagent",
    )
