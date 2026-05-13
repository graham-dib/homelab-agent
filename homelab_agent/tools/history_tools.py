"""History tools — query the metrics DB for time-series questions."""
from __future__ import annotations

import re
from langchain_core.tools import tool

from homelab_agent.ingest.schema import get_connection


SCHEMA_DESCRIPTION = """\
Tables available in the metrics DB (DuckDB syntax):

snapshots(snapshot_id BIGINT, timestamp TIMESTAMP)
  -- One row per metrics snapshot. JOIN other tables to snapshots to get timestamps.

disk_usage(snapshot_id, filesystem, mount, size_gb, used_gb, available_gb, pct_used)
  -- Per filesystem per snapshot. Mounts include '/', '/boot', '/srv/storage'.

memory_stats(snapshot_id, total_mb, used_mb, available_mb, swap_used_mb, pct_used)
  -- One row per snapshot. pct_used is overall memory pressure (0-100).

load_average(snapshot_id, load_1min, load_5min, load_15min, load_pct_1min)
  -- One row per snapshot. load_pct_1min = load_1min / cpu_count * 100.

container_stats(snapshot_id, name, cpu_pct, mem_usage_mb, mem_pct)
  -- One row per running container per snapshot.
  -- name is in {'plex', 'adguard', 'transmission', 'omada'}.

adguard_stats(snapshot_id, num_dns_queries, num_blocked, avg_processing_time)
  -- One row per snapshot. Cumulative counts since AdGuard started, not deltas.
  -- To get queries-per-snapshot, compute differences with LAG().
"""


QUERY_HISTORY_DESCRIPTION = """Query the historical metrics database with read-only SQL.

Use this for temporal questions: 'is X normal?', 'has Y changed over the
last hour?', 'when did Z spike?'. The DB is updated by periodic snapshots.

Args:
    sql: a DuckDB-flavoured SELECT (or WITH ... SELECT) query against the schema
        described below. Read-only — INSERT/UPDATE/DELETE/DROP are rejected.
    max_rows: cap on rows returned (default 100, hard max 1000).

Returns a dict with 'columns' and 'rows', or {'error': ...} on failure.

""" + SCHEMA_DESCRIPTION + """

Examples:

-- Disk usage on /srv/storage over the last 24 hours
SELECT s.timestamp, d.pct_used, d.used_gb
FROM disk_usage d JOIN snapshots s ON s.snapshot_id = d.snapshot_id
WHERE d.mount = '/srv/storage'
  AND s.timestamp > NOW() - INTERVAL '24 hours'
ORDER BY s.timestamp DESC;

-- Container memory averaged over the last hour
SELECT c.name, AVG(c.mem_usage_mb) AS avg_mb, MAX(c.mem_usage_mb) AS peak_mb
FROM container_stats c JOIN snapshots s ON s.snapshot_id = c.snapshot_id
WHERE s.timestamp > NOW() - INTERVAL '1 hour'
GROUP BY c.name
ORDER BY avg_mb DESC;

-- DNS query rate per snapshot (delta from previous snapshot)
SELECT s.timestamp,
       a.num_dns_queries - LAG(a.num_dns_queries) OVER (ORDER BY s.timestamp) AS queries_delta
FROM adguard_stats a JOIN snapshots s ON s.snapshot_id = a.snapshot_id
ORDER BY s.timestamp DESC LIMIT 20;
"""


_ALLOWED_PATTERN = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN_KEYWORDS = ("DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "ATTACH",
                       "CREATE", "PRAGMA", "COPY", "EXPORT", "INSTALL", "LOAD")


def _is_safe_query(sql: str) -> tuple[bool, str]:
    if not _ALLOWED_PATTERN.match(sql):
        return False, "query must start with SELECT or WITH"
    upper = sql.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"forbidden keyword: {kw}"
    return True, ""


@tool(description=QUERY_HISTORY_DESCRIPTION)
def query_history(sql: str, max_rows: int = 100) -> dict:
    """(description set via decorator argument)"""
    ok, reason = _is_safe_query(sql)
    if not ok:
        return {"error": f"rejected: {reason}"}

    max_rows = min(max(int(max_rows), 1), 1000)

    conn = get_connection()
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows)
        columns = [d[0] for d in cur.description]
        clean_rows = []
        for row in rows:
            clean_rows.append([
                v.isoformat() if hasattr(v, "isoformat") else v
                for v in row
            ])
        return {
            "columns": columns,
            "rows": clean_rows,
            "row_count": len(clean_rows),
            "truncated": len(clean_rows) == max_rows,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


@tool
def get_snapshot_coverage() -> dict:
    """Report the time range and density of available historical metrics.

    Returns the earliest and latest snapshot timestamps, the total snapshot
    count, and a rough span estimate. Call this BEFORE making temporal claims —
    if there are only 2 snapshots over 30 seconds, 'trending up over a week'
    is not a valid statement.
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COUNT(*) AS n, MIN(timestamp) AS earliest, MAX(timestamp) AS latest
            FROM snapshots
        """).fetchone()
        n, earliest, latest = row
        if n == 0:
            return {"snapshots": 0, "message": "no snapshots yet"}
        span_seconds = (latest - earliest).total_seconds() if n > 1 else 0
        return {
            "snapshots": n,
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
            "span_seconds": span_seconds,
            "span_human": _humanize_seconds(span_seconds),
        }
    finally:
        conn.close()


def _humanize_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f} min"
    if s < 86400:
        return f"{s/3600:.1f} hours"
    return f"{s/86400:.1f} days"


HISTORY_TOOLS = [query_history, get_snapshot_coverage]