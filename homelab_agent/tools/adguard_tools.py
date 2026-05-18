"""AdGuard Home tools. Wraps the /control REST API with HTTP Basic auth."""
from __future__ import annotations

import base64
from typing import Literal

import httpx
from langchain_core.tools import tool

from homelab_agent.config import settings


def _adguard_client() -> httpx.Client:
    """Authenticated httpx client for AdGuard Home's /control API."""
    auth = base64.b64encode(
        f"{settings.adguard_user}:{settings.adguard_password}".encode()
    ).decode()
    return httpx.Client(
        base_url=f"{settings.adguard_url}/control",
        headers={"Authorization": f"Basic {auth}"},
        timeout=10.0,
    )


@tool
def get_adguard_status() -> dict:
    """Get AdGuard Home server status: version, whether DNS protection is enabled,
    running state, and the DNS port being served.

    Use this as a first-step check that AdGuard is healthy. If 'protection_enabled'
    is False, AdGuard is letting all queries through unfiltered — a real problem.
    """
    with _adguard_client() as c:
        r = c.get("/status")
        r.raise_for_status()
        return r.json()


@tool
def get_adguard_stats() -> dict:
    """Get AdGuard Home DNS query statistics.

    IMPORTANT: all counts (num_dns_queries, num_blocked_filtering, etc.) are a
    rolling 24-hour window that resets continuously — they are NOT cumulative
    since install or since last restart. A count that is lower than a previous
    reading means the window rolled forward and old queries dropped off, not that
    AdGuard restarted or lost data. Never interpret a decrease as a restart.

    Returns total query count, blocked count, replaced safebrowsing/parental/safesearch
    counts, average DNS processing time, top queried domains, top blocked domains,
    and top clients by query volume. Use for questions like 'how many DNS queries
    today', 'what's being blocked', 'which device is making the most queries'.
    """
    with _adguard_client() as c:
        r = c.get("/stats")
        r.raise_for_status()
        return r.json()


@tool
def get_adguard_query_log(limit: int = 50, search: str | None = None) -> list[dict]:
    """Get the most recent DNS queries from AdGuard's query log.

    Args:
        limit: maximum number of log entries to return. Default: 50, max: 500.
        search: optional substring to filter by — searches client IP, queried
            domain, and response. Use this to investigate 'what is device X
            requesting' (pass the IP) or 'has anyone queried domain Y' (pass
            the domain).

    Returns timestamp, client IP/name, queried domain, query type, response,
    whether it was blocked, and which filter list (if any) blocked it.
    """
    params: dict = {"limit": min(limit, 500)}
    if search:
        params["search"] = search
    with _adguard_client() as c:
        r = c.get("/querylog", params=params)
        r.raise_for_status()
        return r.json().get("data", [])


@tool
def get_adguard_top_blocked() -> list[dict]:
    """Get the top domains being blocked by AdGuard right now.

    Returns a list of domains with their query counts. This is a slice of
    get_adguard_stats() but isolated for cleaner consumption when the user
    asks specifically 'what's being blocked'.
    """
    with _adguard_client() as c:
        r = c.get("/stats")
        r.raise_for_status()
        data = r.json()
    return data.get("top_blocked_domains", [])


@tool
def get_adguard_top_clients() -> list[dict]:
    """Get the top clients by DNS query volume over the stats window.

    Returns clients with their query counts. Use to answer 'which device is
    making the most DNS requests' — useful for spotting noisy devices, IoT
    chatter, or unexpected traffic.
    """
    with _adguard_client() as c:
        r = c.get("/stats")
        r.raise_for_status()
        data = r.json()
    return data.get("top_clients", [])


ADGUARD_TOOLS = [
    get_adguard_status,
    get_adguard_stats,
    get_adguard_query_log,
    get_adguard_top_blocked,
    get_adguard_top_clients,
]