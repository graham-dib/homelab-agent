"""DuckDB schema for homelab metrics. Idempotent — safe to run on every startup."""
from __future__ import annotations

from pathlib import Path
import duckdb

# DB lives at the project root, gitignored
DB_PATH = Path(__file__).resolve().parent.parent.parent / "metrics.duckdb"


SCHEMA_SQL = """
-- Each row = one snapshot timestamp. Snapshots are keyed by snapshot_id.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id BIGINT PRIMARY KEY,
    timestamp   TIMESTAMP NOT NULL
);

-- Disk usage per filesystem per snapshot
CREATE TABLE IF NOT EXISTS disk_usage (
    snapshot_id   BIGINT NOT NULL,
    filesystem    VARCHAR NOT NULL,
    mount         VARCHAR NOT NULL,
    size_gb       DOUBLE,
    used_gb       DOUBLE,
    available_gb  DOUBLE,
    pct_used      INTEGER,
    PRIMARY KEY (snapshot_id, mount)
);

-- System memory per snapshot
CREATE TABLE IF NOT EXISTS memory_stats (
    snapshot_id      BIGINT PRIMARY KEY,
    total_mb         INTEGER,
    used_mb          INTEGER,
    available_mb     INTEGER,
    swap_used_mb     INTEGER,
    pct_used         DOUBLE
);

-- Load average per snapshot
CREATE TABLE IF NOT EXISTS load_average (
    snapshot_id   BIGINT PRIMARY KEY,
    load_1min     DOUBLE,
    load_5min     DOUBLE,
    load_15min    DOUBLE,
    load_pct_1min DOUBLE
);

-- One row per container per snapshot
CREATE TABLE IF NOT EXISTS container_stats (
    snapshot_id   BIGINT NOT NULL,
    name          VARCHAR NOT NULL,
    cpu_pct       DOUBLE,
    mem_usage_mb  DOUBLE,
    mem_pct       DOUBLE,
    PRIMARY KEY (snapshot_id, name)
);

-- AdGuard top-level stats per snapshot
CREATE TABLE IF NOT EXISTS adguard_stats (
    snapshot_id          BIGINT PRIMARY KEY,
    num_dns_queries      INTEGER,
    num_blocked          INTEGER,
    avg_processing_time  DOUBLE
);

-- Per-question agent cost tracking
CREATE TABLE IF NOT EXISTS agent_usage (
    id                 BIGINT PRIMARY KEY,
    timestamp          TIMESTAMP NOT NULL,
    question           VARCHAR,
    model              VARCHAR,
    n_llm_calls        INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    estimated_cost_usd DOUBLE,
    latency_seconds    DOUBLE
);

-- Index for time-range queries
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_usage_timestamp ON agent_usage(timestamp);
"""


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a connection to the metrics DB. Creates schema if missing."""
    conn = duckdb.connect(str(DB_PATH))
    conn.execute(SCHEMA_SQL)
    return conn


if __name__ == "__main__":
    # Allow running this file directly to bootstrap the DB
    conn = get_connection()
    print(f"DB initialised at: {DB_PATH}")
    tables = conn.execute(
        "SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()
    print("Tables:")
    for (name,) in tables:
        print(f"  - {name}")
    conn.close()