"""
Infrastructure subagent, responsible for managing and monitoring the infrastructure of the homelab, including servers, network devices, and other hardware components.

Tools:
--- System metrics: disk usage, memory usage, CPU load, network status.
--- Container level resources: list/stats/restart history
--- Does not own Adguard, container logs, plex/transmission/omada intervals

"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain.agents import create_agent

from homelab_agent.tools.system_tools import SYSTEM_TOOLS
from homelab_agent.tools.docker_tools import (

    list_containers,
    get_container_stats,
    find_recently_restarted_containers,
)
from homelab_agent.tools.history_tools import HISTORY_TOOLS
from homelab_agent.tools.storage_tools import STORAGE_TOOLS

INFRA_TOOLS = [
    *SYSTEM_TOOLS,
    list_containers,
    get_container_stats,
    find_recently_restarted_containers,
    *HISTORY_TOOLS,
    *STORAGE_TOOLS,
]

INFRA_PROMPT = """You are the Infrastructure subagent for dibo, a Linux home server.

Your domain:
- Host-level metrics: disk, memory, CPU load, systemd services, journal logs
- Container fleet: which are running, resource consumption, restart history
- Historical trends for the above

NOT your domain (defer back to the supervisor for these):
- DNS / AdGuard internals
- Application logs (Plex, Transmission, Omada)
- Service-specific behaviour inside containers

Guidance:
- Be specific with numbers and units (GB, MB, percentages).
- For diagnostic questions, check multiple relevant signals (disk, memory, load,
  container resources) and synthesise.
- For temporal questions, call get_snapshot_coverage first so you know how much
  history exists. Refuse to claim a trend if the data span is too short.
- /srv/storage at 86% is the known elevated mount — flag it when relevant but
  don't over-emphasise.
- The Java process at ~2GB is Omada Controller (Java + MongoDB pairing).
  Don't misidentify it.
"""

def build_infra_agent(llm=None):
    """Construct the Infrastructure subagent. Returns a runnable graph."""
    if llm is None:
        llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)
    return create_agent(
        model=llm,
        tools=INFRA_TOOLS,
        system_prompt=INFRA_PROMPT,
        name="Infrastructure Subagent",
    )