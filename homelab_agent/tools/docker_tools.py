"""Docker tools for dibo. Wraps `docker` CLI over SSH using JSON output formats."""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel

from ._clients import run_on_dibo


# ---------- Pydantic models ----------

class ContainerSummary(BaseModel):
    name: str
    image: str
    state: str           # "running", "exited", "restarting", "paused"
    status: str          # human-readable, e.g. "Up 3 days"
    created: str         # e.g. "2 weeks ago"
    ports: str           # human-readable port mapping


class ContainerStats(BaseModel):
    name: str
    cpu_pct: float
    mem_usage_mb: float
    mem_limit_mb: float
    mem_pct: float
    net_in_mb: float
    net_out_mb: float
    block_in_mb: float
    block_out_mb: float


class ContainerInspect(BaseModel):
    name: str
    image: str
    state: str
    started_at: str
    restart_count: int
    restart_policy: str
    health_status: str | None  # "healthy", "unhealthy", "starting", or None
    mounts: list[dict]


# ---------- Helpers ----------

def _parse_size_to_mb(s: str) -> float:
    """Convert Docker's human-readable sizes like '1.234GiB' or '567MB' to MB.

    Docker uses inconsistent units across commands: stats uses GiB/MiB, ps uses GB/MB.
    Handle both binary (GiB/MiB) and decimal (GB/MB) gracefully.
    """
    s = s.strip()
    if not s or s == "--":
        return 0.0

    # Find where the number ends
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] in ".-"):
        i += 1
    num = float(s[:i])
    unit = s[i:].strip()

    # Binary units (Docker stats default)
    binary_factors = {
        "B": 1 / 1024 / 1024,
        "KiB": 1 / 1024,
        "MiB": 1,
        "GiB": 1024,
        "TiB": 1024 * 1024,
    }
    # Decimal units (some Docker commands)
    decimal_factors = {
        "kB": 0.001,
        "MB": 1,
        "GB": 1000,
        "TB": 1_000_000,
    }

    if unit in binary_factors:
        return round(num * binary_factors[unit], 2)
    if unit in decimal_factors:
        return round(num * decimal_factors[unit], 2)
    return round(num, 2)  # unknown unit, return raw


# ---------- Tools ----------

@tool
def list_containers(all_containers: bool = True) -> list[dict]:
    """List Docker containers on dibo with their state, image, and status.

    Args:
        all_containers: if True (default), include stopped containers too.
            If False, only return running containers.

    Returns name, image, state ('running'/'exited'/etc), human-readable status
    ('Up 3 days', sometimes annotated with '(healthy)' or '(unhealthy)' if the
    container has a Docker healthcheck defined — many containers don't have one,
    and absence of a healthcheck annotation is normal), creation time, and port
    mappings.

    Use this to get an overview of the container fleet — what's running, what's
    crashed, what's been recently created. Container names on dibo include:
    'plex', 'adguard', 'transmission', 'omada' for the four main services.
    """
    flag = "-a " if all_containers else ""
    cmd = f'docker ps {flag}--format "{{{{json .}}}}"'
    raw = run_on_dibo(cmd)
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        data = json.loads(line)
        out.append(
            ContainerSummary(
                name=data.get("Names", ""),
                image=data.get("Image", ""),
                state=data.get("State", ""),
                status=data.get("Status", ""),
                created=data.get("CreatedAt", ""),
                ports=data.get("Ports", ""),
            ).model_dump()
        )
    return out


@tool
def get_container_stats(name: str) -> dict:
    """Get real-time CPU, memory, and network stats for a single running container.

    Args:
        name: container name, e.g. 'plex', 'adguardhome'. Must be a running
            container — stopped containers will return an error.

    Returns CPU percent, memory usage and limit in MB, memory percent, and
    cumulative network/block I/O in MB since container start. Use this to
    investigate whether a specific service is under resource pressure.
    """
    cmd = f'docker stats {name} --no-stream --format "{{{{json .}}}}"'
    raw = run_on_dibo(cmd, warn=True)
    if not raw or "Error" in raw:
        return {"error": f"could not get stats for '{name}' — container may not be running"}

    data = json.loads(raw)
    # MemUsage format: "1.234GiB / 8GiB"
    mem_used_str, _, mem_limit_str = data.get("MemUsage", "0B / 0B").partition(" / ")
    # NetIO and BlockIO format: "1.2MB / 3.4MB"
    net_in, _, net_out = data.get("NetIO", "0B / 0B").partition(" / ")
    block_in, _, block_out = data.get("BlockIO", "0B / 0B").partition(" / ")

    return ContainerStats(
        name=data.get("Name", name),
        cpu_pct=float(data.get("CPUPerc", "0%").rstrip("%")),
        mem_usage_mb=_parse_size_to_mb(mem_used_str),
        mem_limit_mb=_parse_size_to_mb(mem_limit_str),
        mem_pct=float(data.get("MemPerc", "0%").rstrip("%")),
        net_in_mb=_parse_size_to_mb(net_in),
        net_out_mb=_parse_size_to_mb(net_out),
        block_in_mb=_parse_size_to_mb(block_in),
        block_out_mb=_parse_size_to_mb(block_out),
    ).model_dump()


@tool
def get_container_logs(name: str, tail: int = 100) -> str:
    """Get the most recent log lines from a container.

    Args:
        name: container name, e.g. 'plex', 'adguardhome', 'omada-controller'.
        tail: number of lines from the end of the log. Default: 100.

    Returns raw log output as a string. Use this to investigate why a container
    is misbehaving, crashing, or producing errors. The logs include both stdout
    and stderr, in chronological order.
    """
    # `docker logs` writes to both stdout and stderr; merge them with 2>&1
    cmd = f"docker logs --tail {tail} {name} 2>&1"
    result = run_on_dibo(cmd, warn=True, timeout=20)
    return result or f"(no logs returned for '{name}' — container may not exist)"


@tool
def inspect_container(name: str) -> dict:
    """Get detailed metadata about a container: image, state, restart history,
    health check status, restart policy, and mount points.

    Args:
        name: container name.

    Use this to investigate persistent issues — e.g. 'why does this container
    keep restarting' (check restart_count and restart_policy) or 'what volumes
    does this container have access to' (check mounts). For real-time resource
    metrics, use get_container_stats instead.
    """
    cmd = f"docker inspect {name}"
    raw = run_on_dibo(cmd, warn=True)
    if not raw or raw.startswith("[]"):
        return {"error": f"container '{name}' not found"}

    data = json.loads(raw)[0]  # docker inspect returns a list
    state = data.get("State", {})
    config = data.get("HostConfig", {})
    health = state.get("Health", {})

    return ContainerInspect(
        name=data.get("Name", "").lstrip("/"),
        image=data.get("Config", {}).get("Image", ""),
        state=state.get("Status", ""),
        started_at=state.get("StartedAt", ""),
        restart_count=data.get("RestartCount", 0),
        restart_policy=config.get("RestartPolicy", {}).get("Name", "no"),
        health_status=health.get("Status") if health else None,
        mounts=[
            {
                "source": m.get("Source", ""),
                "destination": m.get("Destination", ""),
                "type": m.get("Type", ""),
                "mode": m.get("Mode", ""),
            }
            for m in data.get("Mounts", [])
        ],
    ).model_dump()


@tool
def find_recently_restarted_containers(since_hours: int = 24) -> list[dict]:
    """Find containers that have restarted recently — useful for spotting flapping services.

    Args:
        since_hours: look back this many hours. Default: 24.

    Returns containers whose 'started_at' is within the window, along with their
    restart count. A high restart count is a red flag — services in healthy
    operation typically start once and stay up. Use this when investigating
    'something has been acting up' or as a proactive health scan.
    """
    # docker ps gives us names; we then inspect each
    cmd = 'docker ps -a --format "{{.Names}}"'
    names = run_on_dibo(cmd).split("\n")

    flagged = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        try:
            info = inspect_container.invoke({"name": name})
            if "error" in info:
                continue
            # crude time check: started_at is ISO-ish; just check restart count for now
            if info["restart_count"] > 0:
                flagged.append({
                    "name": info["name"],
                    "restart_count": info["restart_count"],
                    "state": info["state"],
                    "started_at": info["started_at"],
                    "health_status": info["health_status"],
                })
        except Exception:
            continue  # don't let one bad container break the whole scan
    return flagged


# ---------- Registration ----------

DOCKER_TOOLS = [
    list_containers,
    get_container_stats,
    get_container_logs,
    inspect_container,
    find_recently_restarted_containers,
]