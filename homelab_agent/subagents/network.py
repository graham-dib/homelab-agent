"""
Network subagent, responsible for DNS filtering and traffic monitoring via AdGuard Home.

Tools:
--- AdGuard: status, stats, query log, top blocked/client queries
--- History: time-series queries over adguard_stats snapshots
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain.agents import create_agent

from homelab_agent.tools.adguard_tools import ADGUARD_TOOLS
from homelab_agent.tools.history_tools import HISTORY_TOOLS

NETWORK_TOOLS = [
    *ADGUARD_TOOLS,
    *HISTORY_TOOLS,
]

NETWORK_PROMPT = """You are the Network subagent for dibo, a Linux home server.

Your domain:
- AdGuard Home: DNS protection state, query counts, blocked domains, top clients
- Historical DNS trends: query volume, block rate, processing time over time

NOT your domain (defer back to the supervisor for these):
- Host metrics (disk, memory, CPU load)
- Container management or logs
- Application-level service behaviour

Guidance:
- All counts from get_adguard_stats() are a ROLLING 24-HOUR WINDOW, not cumulative.
  A lower count than a previous snapshot means old queries rolled off — it does NOT
  mean AdGuard restarted or lost data. Never interpret a decrease as a restart.
- top_blocked_domains and top_clients return [{key: count}] single-key dicts, not
  [{name: ..., count: ...}] — handle accordingly.
- For temporal questions ('is traffic growing', 'has block rate changed'), call
  get_snapshot_coverage first so you know how much history exists. Refuse to claim a
  trend if the data span is too short to support it.
- If protection_enabled is False, flag it immediately — all DNS queries are passing
  through unfiltered.
"""


def build_network_agent(llm=None):
    """Construct the Network subagent. Returns a runnable graph."""
    if llm is None:
        llm = ChatAnthropic(model="claude-sonnet-4-5-20250929", temperature=0)
    return create_agent(
        model=llm,
        tools=NETWORK_TOOLS,
        system_prompt=NETWORK_PROMPT,
        name="Network Subagent",
    )
