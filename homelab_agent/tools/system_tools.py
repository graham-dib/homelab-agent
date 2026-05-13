"""System-level tools for the dibo host: disk, memory, CPU, services, logs, processes."""
from __future__ import annotations

from typing import Literal
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ._clients import run_on_dibo


# ---------- Pydantic models for typed returns ----------

class DiskUsage(BaseModel):
    filesystem: str
    size_gb: float
    used_gb: float
    available_gb: float
    pct_used: int
    mount: str


class MemoryStats(BaseModel):
    total_mb: int
    used_mb: int
    free_mb: int
    available_mb: int
    buffers_cache_mb: int
    swap_total_mb: int
    swap_used_mb: int
    pct_used: float


class LoadAverage(BaseModel):
    uptime_human: str
    users_logged_in: int
    load_1min: float
    load_5min: float
    load_15min: float
    cpu_count: int
    load_pct_1min: float = Field(
        description="Load average as percentage of CPU capacity (load_1min / cpu_count * 100)"
    )


class ServiceStatus(BaseModel):
    unit: str
    active_state: str
    sub_state: str
    is_running: bool
    main_pid: int | None
    memory_mb: float | None
    uptime_seconds: int | None


class ProcessInfo(BaseModel):
    pid: int
    user: str
    cpu_pct: float
    mem_pct: float
    rss_mb: float
    command: str


# ---------- Tools ----------

@tool
def get_disk_usage() -> list[dict]:
    """Return disk usage for all mounted filesystems on dibo.

    Reports filesystem device, total size in GB, used GB, available GB,
    percent used, and mount point. Use this to diagnose disk-pressure
    issues — anything above 85% used warrants attention; above 95% is critical.
    Excludes pseudo-filesystems (tmpfs, devtmpfs, overlay).
    """
    # -B1G forces output in GB; --output gives stable columns
    raw = run_on_dibo(
        "df -B1G --output=source,size,used,avail,pcent,target "
        "-x tmpfs -x devtmpfs -x overlay -x squashfs"
    )
    lines = raw.split("\n")[1:]  # skip header
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        # Each size value is like "2097152K" - strip the K, divide for GB
        size_kb = int(parts[1].rstrip("K"))
        used_kb = int(parts[2].rstrip("K"))
        avail_kb = int(parts[3].rstrip("K"))
        out.append(
            DiskUsage(
                filesystem=parts[0],
                size_gb=round(size_kb / 1024 / 1024, 2),
                used_gb=round(used_kb / 1024 / 1024, 2),
                available_gb=round(avail_kb / 1024 / 1024, 2),
                pct_used=int(parts[4].rstrip("%")),
                mount=parts[5],
            ).model_dump()
        )
    return out



@tool
def get_memory_stats() -> dict:
    """Return RAM and swap usage on dibo.

    Reports total, used, free, available memory in MB plus swap usage.
    'available' is the more useful metric than 'free' on Linux — it accounts
    for cached memory that can be reclaimed. Percent used above 90% with
    swap also growing indicates real memory pressure.
    """
    raw = run_on_dibo("free -m")
    # free -m output:
    #               total  used   free   shared  buff/cache  available
    # Mem:           7837   1234   456    123     6147        6234
    # Swap:          2047   0      2047
    lines = raw.split("\n")
    mem_parts = lines[1].split()
    swap_parts = lines[2].split()

    total = int(mem_parts[1])
    used = int(mem_parts[2])
    free = int(mem_parts[3])
    buffers_cache = int(mem_parts[5])
    available = int(mem_parts[6])

    return MemoryStats(
        total_mb=total,
        used_mb=used,
        free_mb=free,
        available_mb=available,
        buffers_cache_mb=buffers_cache,
        swap_total_mb=int(swap_parts[1]),
        swap_used_mb=int(swap_parts[2]),
        pct_used=round((total - available) / total * 100, 1),
    ).model_dump()


@tool
def get_load_average() -> dict:
    """Return system load averages and uptime for dibo.

    Load averages over 1, 5, and 15 minutes — these represent the number
    of processes runnable or in uninterruptible sleep, averaged over time.
    Also includes CPU count, so the ratio of load to CPU capacity is meaningful:
    load_pct_1min > 100 means processes are queueing; > 200 is significant pressure.
    Includes a human-readable uptime string.
    """
    raw = run_on_dibo("uptime")
    # Format:  12:34:56 up 3 days,  2:14,  2 users,  load average: 0.45, 0.50, 0.60
    parts = raw.split("load average:")
    head = parts[0]
    loads = [float(x.strip().rstrip(",")) for x in parts[1].split(",")]

    # extract "up X days, Y:ZZ"
    uptime_part = head.split("up", 1)[1]
    uptime_human = uptime_part.split(",  ", 1)[0].strip()

    # extract user count
    users = 0
    for token in head.split(","):
        if "user" in token:
            users = int(token.strip().split()[0])
            break

    # cpu count
    cpu_count = int(run_on_dibo("nproc"))

    return LoadAverage(
        uptime_human=uptime_human,
        users_logged_in=users,
        load_1min=loads[0],
        load_5min=loads[1],
        load_15min=loads[2],
        cpu_count=cpu_count,
        load_pct_1min=round(loads[0] / cpu_count * 100, 1),
    ).model_dump()


@tool
def get_service_status(unit: str) -> dict:
    """Get the status of a systemd service on dibo.

    Args:
        unit: systemd unit name, e.g. 'docker', 'ssh', 'tailscaled', 'cron'.
            Do not include the .service suffix unless the unit is non-service
            (e.g. .timer, .socket).

    Returns active_state ('active', 'inactive', 'failed'), sub_state
    ('running', 'dead', 'exited'), main PID, memory consumption, and uptime.
    is_running is True only when active_state == 'active' and sub_state == 'running'.
    """
    # systemctl show gives machine-parseable key=value output
    fields = "ActiveState,SubState,MainPID,MemoryCurrent,ActiveEnterTimestampMonotonic"
    raw = run_on_dibo(
        f"systemctl show {unit} --property={fields} --no-pager",
        warn=True,
    )
    if not raw:
        return ServiceStatus(
            unit=unit,
            active_state="unknown",
            sub_state="unknown",
            is_running=False,
            main_pid=None,
            memory_mb=None,
            uptime_seconds=None,
        ).model_dump()

    kv = dict(line.split("=", 1) for line in raw.split("\n") if "=" in line)

    main_pid = int(kv.get("MainPID", "0")) or None

    mem_bytes_str = kv.get("MemoryCurrent", "")
    memory_mb = (
        round(int(mem_bytes_str) / 1024 / 1024, 1)
        if mem_bytes_str.isdigit() and mem_bytes_str != "[not set]"
        else None
    )

    # ActiveEnterTimestampMonotonic is microseconds since boot; convert to uptime
    enter_us_str = kv.get("ActiveEnterTimestampMonotonic", "0")
    uptime_seconds: int | None = None
    if enter_us_str.isdigit() and enter_us_str != "0":
        # monotonic now (seconds since boot)
        now_s = float(run_on_dibo("cat /proc/uptime").split()[0])
        enter_s = int(enter_us_str) / 1_000_000
        uptime_seconds = max(0, int(now_s - enter_s))

    active = kv.get("ActiveState", "unknown")
    sub = kv.get("SubState", "unknown")

    return ServiceStatus(
        unit=unit,
        active_state=active,
        sub_state=sub,
        is_running=(active == "active" and sub == "running"),
        main_pid=main_pid,
        memory_mb=memory_mb,
        uptime_seconds=uptime_seconds,
    ).model_dump()


@tool
def get_journal_logs(
    unit: str,
    since: str = "1 hour ago",
    priority: Literal["err", "warning", "info", "debug"] = "warning",
    lines: int = 50,
) -> str:
    """Get systemd journal logs for a service, filtered by severity and time.

    Args:
        unit: systemd unit name, e.g. 'docker', 'ssh'. Do not include .service.
        since: human-readable time string accepted by journalctl, e.g.
            '1 hour ago', '2024-01-15 14:00', 'yesterday', '30 min ago'.
            Default: '1 hour ago'.
        priority: minimum severity. 'err' shows only errors; 'warning' shows
            warnings and errors; 'info' includes info; 'debug' is everything.
            Default: 'warning'.
        lines: maximum number of log lines to return. Default: 50.

    Returns the raw log output as a string. Use this to investigate why a
    service is misbehaving — pair with get_service_status to first confirm
    the service state, then read logs for context.
    """
    # priority levels: emerg=0, alert=1, crit=2, err=3, warning=4, notice=5, info=6, debug=7
    priority_map = {"err": 3, "warning": 4, "info": 6, "debug": 7}
    p = priority_map[priority]

    cmd = (
        f"journalctl -u {unit} "
        f'--since="{since}" '
        f"--priority={p} "
        f"--no-pager "
        f"-n {lines}"
    )
    return run_on_dibo(cmd, warn=True, timeout=20) or "(no log entries match)"


@tool
def get_top_processes(by: Literal["cpu", "memory"] = "cpu", n: int = 10) -> list[dict]:
    """List the top N processes on dibo, sorted by CPU or memory consumption.

    Args:
        by: 'cpu' to sort by CPU usage percent, 'memory' to sort by resident
            memory size. Default: 'cpu'.
        n: number of processes to return. Default: 10.

    Returns PID, owning user, CPU percent, memory percent, RSS in MB, and the
    command. Use this to find runaway processes when memory or CPU is under
    pressure. Note that 'cpu_pct' is a snapshot — for sustained usage,
    cross-reference with load averages.
    """
    sort_field = "-%cpu" if by == "cpu" else "-%mem"
    # ps -e: all processes; -o: custom columns; --sort: by field
    cmd = (
        f"ps -eo pid,user,%cpu,%mem,rss,comm "
        f"--sort={sort_field} --no-headers | head -n {n}"
    )
    raw = run_on_dibo(cmd)
    out = []
    for line in raw.split("\n"):
        parts = line.split(None, 5)  # split on whitespace, max 6 parts (keep command intact)
        if len(parts) < 6:
            continue
        out.append(
            ProcessInfo(
                pid=int(parts[0]),
                user=parts[1],
                cpu_pct=float(parts[2]),
                mem_pct=float(parts[3]),
                rss_mb=round(int(parts[4]) / 1024, 1),  # ps rss is in KB
                command=parts[5],
            ).model_dump()
        )
    return out


@tool
def check_dibo_reachable() -> dict:
    """Verify dibo is reachable over SSH and return basic identity info.

    Returns hostname, kernel version, uptime in seconds, and a 'reachable' bool.
    Use this as a first-step health check before running other tools, or when
    a user asks 'is dibo up'. Cheap to call.
    """
    try:
        hostname = run_on_dibo("hostname", timeout=5)
        kernel = run_on_dibo("uname -r", timeout=5)
        uptime_s = float(run_on_dibo("cat /proc/uptime", timeout=5).split()[0])
        return {
            "reachable": True,
            "hostname": hostname,
            "kernel": kernel,
            "uptime_seconds": int(uptime_s),
        }
    except Exception as e:
        return {
            "reachable": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
        }


# ---------- Convenience list for registering with the agent ----------

SYSTEM_TOOLS = [
    check_dibo_reachable,
    get_disk_usage,
    get_memory_stats,
    get_load_average,
    get_service_status,
    get_journal_logs,
    get_top_processes,
]