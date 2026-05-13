"""Take one snapshot of dibo's state and write it to the metrics DB.

Usage:
    python -m homelab_agent.ingest.snapshot

Designed to be run on a cron schedule (every 5 minutes). Idempotent: each
invocation creates a new snapshot_id from the wall-clock time.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from homelab_agent.ingest.schema import get_connection
from homelab_agent.tools.system_tools import (
    get_disk_usage,
    get_memory_stats,
    get_load_average,
)
from homelab_agent.tools.docker_tools import list_containers, get_container_stats
from homelab_agent.tools.adguard_tools import get_adguard_stats


def take_snapshot() -> int:
    """Take one full snapshot. Returns the snapshot_id used."""
    # snapshot_id = unix epoch in seconds — naturally unique and chronological
    snapshot_id = int(time.time())
    timestamp = datetime.now(timezone.utc)

    conn = get_connection()

    try:
        # ----- 1. snapshots header row -----
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, timestamp) VALUES (?, ?)",
            [snapshot_id, timestamp],
        )

        # ----- 2. disk usage -----
        try:
            disks = get_disk_usage.invoke({})
            for d in disks:
                conn.execute(
                    """INSERT INTO disk_usage
                       (snapshot_id, filesystem, mount, size_gb, used_gb, available_gb, pct_used)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [snapshot_id, d["filesystem"], d["mount"],
                     d["size_gb"], d["used_gb"], d["available_gb"], d["pct_used"]],
                )
        except Exception as e:
            print(f"  ! disk_usage failed: {type(e).__name__}: {e}")

        # ----- 3. memory -----
        try:
            mem = get_memory_stats.invoke({})
            conn.execute(
                """INSERT INTO memory_stats
                   (snapshot_id, total_mb, used_mb, available_mb, swap_used_mb, pct_used)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [snapshot_id, mem["total_mb"], mem["used_mb"], mem["available_mb"],
                 mem["swap_used_mb"], mem["pct_used"]],
            )
        except Exception as e:
            print(f"  ! memory_stats failed: {type(e).__name__}: {e}")

        # ----- 4. load average -----
        try:
            load = get_load_average.invoke({})
            conn.execute(
                """INSERT INTO load_average
                   (snapshot_id, load_1min, load_5min, load_15min, load_pct_1min)
                   VALUES (?, ?, ?, ?, ?)""",
                [snapshot_id, load["load_1min"], load["load_5min"],
                 load["load_15min"], load["load_pct_1min"]],
            )
        except Exception as e:
            print(f"  ! load_average failed: {type(e).__name__}: {e}")

        # ----- 5. container stats (for running containers only) -----
        try:
            containers = list_containers.invoke({"all_containers": False})
            for c in containers:
                try:
                    stats = get_container_stats.invoke({"name": c["name"]})
                    if "error" in stats:
                        continue
                    conn.execute(
                        """INSERT INTO container_stats
                           (snapshot_id, name, cpu_pct, mem_usage_mb, mem_pct)
                           VALUES (?, ?, ?, ?, ?)""",
                        [snapshot_id, stats["name"], stats["cpu_pct"],
                         stats["mem_usage_mb"], stats["mem_pct"]],
                    )
                except Exception as e:
                    print(f"  ! container_stats[{c['name']}] failed: {e}")
        except Exception as e:
            print(f"  ! list_containers failed: {type(e).__name__}: {e}")

        # ----- 6. AdGuard stats -----
        try:
            ag = get_adguard_stats.invoke({})
            conn.execute(
                """INSERT INTO adguard_stats
                   (snapshot_id, num_dns_queries, num_blocked, avg_processing_time)
                   VALUES (?, ?, ?, ?)""",
                [snapshot_id,
                 ag.get("num_dns_queries", 0),
                 ag.get("num_blocked_filtering", 0),
                 ag.get("avg_processing_time", 0.0)],
            )
        except Exception as e:
            print(f"  ! adguard_stats failed: {type(e).__name__}: {e}")

        conn.commit()
    finally:
        conn.close()

    return snapshot_id


if __name__ == "__main__":
    print(f"Taking snapshot at {datetime.now().isoformat(timespec='seconds')}...")
    start = time.time()
    snap_id = take_snapshot()
    elapsed = time.time() - start
    print(f"Snapshot {snap_id} written in {elapsed:.1f}s")

    # Quick sanity check: count rows we just wrote
    conn = get_connection()
    print("\nRows in this snapshot:")
    for table in ["disk_usage", "memory_stats", "load_average",
                  "container_stats", "adguard_stats"]:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE snapshot_id = ?", [snap_id]
        ).fetchone()[0]
        print(f"  {table:20s} {count}")
    conn.close()