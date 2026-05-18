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
import shlex

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


@tool
def kill_torrent(torrent_id: str) -> str:
    """Stop (pause) an active torrent in Transmission.

    This pauses the torrent — download and upload cease but the torrent and its
    data are kept. The operator can resume it manually at any time. Use when a
    torrent is consuming too much bandwidth or needs to be held.
    REQUIRES HUMAN APPROVAL before executing.

    Args:
        torrent_id: Transmission torrent ID or hash (use get_container_logs on
                    'transmission' and look for the torrent list, or use
                    'transmission-remote localhost -l' output)
    """
    decision = interrupt({
        "action": "kill_torrent",
        "args": {"torrent_id": torrent_id},
        "warning": f"Will stop (pause) torrent {torrent_id!r} in Transmission. Data is kept; torrent can be resumed.",
        "reversible": True,
    })
    if decision != "approved":
        return f"[CANCELLED] kill_torrent(torrent_id={torrent_id!r}) was not approved by the operator."
    result = run_on_dibo(
        f"docker exec transmission transmission-remote localhost:9091 -t {torrent_id} --stop",
        timeout=15,
    )
    return f"Torrent {torrent_id!r} stopped. Result: {result or 'OK'}"


@tool
def enable_adguard_protection(enabled: bool) -> str:
    """Enable or disable AdGuard Home DNS protection.

    When disabled, AdGuard passes all DNS queries through to upstream resolvers
    without filtering — no ads or domains are blocked. Use to temporarily lift
    filtering (e.g. to diagnose a false positive), then re-enable promptly.
    REQUIRES HUMAN APPROVAL before executing.

    Args:
        enabled: True to turn protection on, False to turn it off.
    """
    state = "enable" if enabled else "DISABLE"
    warning = (
        "Turns AdGuard DNS protection ON — normal filtering resumes."
        if enabled
        else "DISABLES AdGuard DNS filtering — all DNS queries pass through unblocked."
    )
    decision = interrupt({
        "action": "enable_adguard_protection",
        "args": {"enabled": enabled},
        "warning": warning,
        "reversible": True,
    })
    if decision != "approved":
        return f"[CANCELLED] enable_adguard_protection(enabled={enabled}) was not approved by the operator."
    with _adguard_client() as c:
        c.post("/protection", json={"enabled": enabled}).raise_for_status()
    return f"AdGuard DNS protection {'enabled' if enabled else 'disabled'} successfully."


@tool
def set_download_limit(limit_kb: int) -> str:
    """Set the global download speed limit in Transmission.

    Applies to all active torrents immediately. Set to 0 to remove the limit.
    Useful when dibo's bandwidth is needed for other tasks (streaming, backups)
    or when a torrent is saturating the connection.
    REQUIRES HUMAN APPROVAL before executing.

    Args:
        limit_kb: Download speed cap in KB/s. 0 = unlimited.
    """
    if limit_kb < 0:
        return "Error: limit_kb must be 0 (unlimited) or a positive number."

    if limit_kb == 0:
        display = "unlimited (remove limit)"
        cmd = "docker exec transmission transmission-remote localhost:9091 --no-downlimit"
    else:
        display = f"{limit_kb} KB/s"
        cmd = f"docker exec transmission transmission-remote localhost:9091 --downlimit {limit_kb}"

    decision = interrupt({
        "action": "set_download_limit",
        "args": {"limit_kb": limit_kb},
        "warning": f"Will set Transmission global download limit to {display}. Takes effect immediately on all active torrents.",
        "reversible": True,
    })
    if decision != "approved":
        return f"[CANCELLED] set_download_limit(limit_kb={limit_kb}) was not approved by the operator."
    result = run_on_dibo(cmd, timeout=10)
    return f"Download limit set to {display}. Result: {result or 'OK'}"


@tool
def delete_files(paths: list[str]) -> str:
    """Permanently delete one or more files on dibo.

    Use after identifying cleanup candidates via find_large_files or
    find_old_files. Present the full list clearly before calling this tool —
    the operator must approve before anything is removed.
    REQUIRES HUMAN APPROVAL. Deletion is irreversible.

    Args:
        paths: List of absolute file paths to delete (e.g.
               ['/srv/storage/transmission/downloads/old.mkv'])
    """
    if not paths:
        return "Error: no paths provided."

    # Reject anything outside /srv to prevent accidental system damage
    for p in paths:
        if not p.startswith("/srv/"):
            return f"Error: path {p!r} is outside /srv/ — refusing for safety."

    # Get sizes to show in the approval prompt
    quoted = " ".join(shlex.quote(p) for p in paths)
    total = run_on_dibo(
        f"du -shc {quoted} 2>/dev/null | tail -1 | awk '{{print $1}}'",
        timeout=20,
    ) or "unknown"

    decision = interrupt({
        "action": "delete_files",
        "args": {"paths": paths},
        "warning": (
            f"Will permanently delete {len(paths)} file(s) "
            f"({total} total). THIS CANNOT BE UNDONE.\n"
            + "\n".join(f"  {p}" for p in paths)
        ),
        "reversible": False,
    })
    if decision != "approved":
        return "[CANCELLED] delete_files was not approved by the operator."

    result = run_on_dibo(f"rm -f {quoted}", timeout=60)
    return f"Deleted {len(paths)} file(s), freed approximately {total}."


WRITE_TOOLS = [
    restart_container,
    flush_adguard_cache,
    reboot_dibo,
    kill_torrent,
    enable_adguard_protection,
    set_download_limit,
    delete_files,
]
