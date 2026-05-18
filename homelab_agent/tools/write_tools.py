"""Write tools for dibo — actions that mutate state.

Every tool here calls interrupt() before executing. The graph pauses, the CLI
surfaces the proposal to the operator, and the operator approves or rejects.
On approval the graph resumes and the tool executes; on rejection it returns a
cancellation message without touching anything.

LangGraph 1.x supports interrupt() inside @tool functions: GraphInterrupt
propagates through ToolNode as GraphBubbleUp and is re-raised to the graph
runtime, which saves the checkpoint and suspends execution.
"""
from __future__ import annotations

import json

import httpx
from langchain_core.tools import tool
from langgraph.types import interrupt

from homelab_agent.tools._clients import run_on_dibo


def _adguard_client() -> httpx.Client:
    """Authenticated httpx client for AdGuard Home's /control API."""
    import base64
    from homelab_agent.config import settings
    auth = base64.b64encode(
        f"{settings.adguard_user}:{settings.adguard_password}".encode()
    ).decode()
    return httpx.Client(
        base_url=f"{settings.adguard_url}/control",
        headers={"Authorization": f"Basic {auth}"},
        timeout=10.0,
    )


@tool
def restart_container(name: str) -> str:
    """Restart a named Docker container on dibo.

    Use this when a container is unhealthy, stuck, or explicitly requested by
    the operator. The container will be offline briefly during restart.
    REQUIRES HUMAN APPROVAL before executing.

    Args:
        name: container name as shown by list_containers (e.g. 'plex', 'adguard')
    """
    decision = interrupt({
        "action": "restart_container",
        "args": {"name": name},
        "warning": f"Will run `docker restart {name}` — the container will be briefly offline.",
        "reversible": True,
    })
    if decision != "approved":
        return f"[CANCELLED] restart_container(name={name!r}) was not approved by the operator."
    run_on_dibo(f"docker restart {name}", timeout=30)
    return f"Container '{name}' restarted successfully."


@tool
def flush_adguard_cache() -> str:
    """Flush AdGuard Home's DNS response cache.

    Clears all cached DNS responses — subsequent queries will be slightly slower
    until the cache repopulates. Useful when a domain's IP has changed and
    clients are getting stale cached answers.
    REQUIRES HUMAN APPROVAL before executing.
    """
    decision = interrupt({
        "action": "flush_adguard_cache",
        "args": {},
        "warning": "Clears AdGuard's DNS cache. Queries will hit upstream DNS until repopulated (~minutes).",
        "reversible": True,
    })
    if decision != "approved":
        return "[CANCELLED] flush_adguard_cache() was not approved by the operator."
    with _adguard_client() as c:
        c.post("/cache_clear").raise_for_status()
    return "AdGuard DNS cache flushed successfully."


@tool
def reboot_dibo() -> str:
    """Reboot the entire dibo server.

    ALL services (Plex, AdGuard, Transmission, Omada) will be offline for
    approximately 2 minutes. Use only as a last resort when other remediation
    has failed or when a kernel/system update requires a restart.
    REQUIRES HUMAN APPROVAL before executing.
    """
    decision = interrupt({
        "action": "reboot_dibo",
        "args": {},
        "warning": "FULL SERVER REBOOT — all services offline for ~2 minutes. Use only as a last resort.",
        "reversible": True,
    })
    if decision != "approved":
        return "[CANCELLED] reboot_dibo() was not approved by the operator."
    run_on_dibo("sudo reboot", warn=True, timeout=10)
    return "Reboot command sent. dibo will be offline for ~2 minutes."


WRITE_TOOLS = [
    restart_container,
    flush_adguard_cache,
    reboot_dibo,
]
