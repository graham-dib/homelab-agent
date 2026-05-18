# dibo-agent

A production-pattern multi-agent system for observing, diagnosing, and acting on a home server. Built as a portfolio piece demonstrating patterns relevant to front-office AI agents at financial institutions: bounded tool scopes, human-in-the-loop approval gates, audit logging, cost tracking, and on-prem inference benchmarking.

The homelab is a vehicle. The patterns are the point.

---

## Architecture

```
                         ┌──────────────────┐
   User query ──────────▶│    Supervisor     │──────▶ Final answer
                         └────────┬─────────┘
               ┌──────────────────┼──────────────────┐
               ▼                  ▼                  ▼
          ┌─────────┐       ┌─────────┐       ┌─────────┐
          │  Infra  │       │ Network │       │  Media  │
          └─────────┘       └─────────┘       └─────────┘
          disk/mem/CPU       DNS/AdGuard      Plex/Torrent
          containers         query logs        /Omada logs
          systemd svc        block rates       health state
          history tools      history tools     history tools

                         ┌──────────────────┐
                         │   Write Tools    │  ← supervisor-level
                         │ restart_container│
                         │ flush_adguard_   │  interrupt() → human
                         │   cache          │  approval gate
                         │ reboot_dibo      │
                         └──────────────────┘
```

**Stack:** Python 3.12 · LangGraph 1.2 + LangChain 1.3 · `langgraph-supervisor` 0.0.31 · Claude Sonnet 4.5 · Fabric/paramiko (SSH) · httpx (AdGuard REST) · DuckDB (metrics store) · pydantic-settings

**Server:** "dibo" — 2017 MacBook Pro running Ubuntu 24, hosting Plex, AdGuard Home, Transmission, and the TP-Link Omada controller in Docker.

---

## Key design decisions

### 1. Bounded tool scopes per subagent

Each subagent owns a specific domain and cannot call tools outside it. The Infra subagent cannot query AdGuard; the Network subagent cannot restart containers. This is enforced structurally — not by prompt alone — because each subagent is built with an explicit tool list.

This mirrors how front-office agent systems at banks are typically scoped: a risk subagent should not be able to execute trades, even if a prompt says "don't."

### 2. Human-in-the-loop via LangGraph `interrupt()`

Write tools call `interrupt()` before executing. The graph checkpoints state, surfaces the proposed action to the operator, and waits. On approval it re-executes the tool and proceeds; on rejection it returns a cancellation message without touching anything.

```
LLM proposes action → interrupt() → graph pauses → operator sees:
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    APPROVAL REQUIRED
    Action : restart_container
    Args   : {"name": "plex"}
    Warning: Will run `docker restart plex` — briefly offline.
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  Approve? [y/n]:
→ 'y' → tool re-executes → action taken
→ 'n' → [CANCELLED] returned → supervisor acknowledges
```

Every proposal and decision is written to the `action_proposals` audit table immediately, regardless of outcome.

### 3. Temporal humility

Before any claim involving trends or change over time, the agent calls `get_snapshot_coverage` to find out how much history exists. If the data span is too short to support the claim, it says so explicitly rather than guessing. This came from a real incident: in early runs the agent claimed AdGuard trends with only 1.7 hours of data.

### 4. Read-only SQL with an allowlist

`query_history` accepts user-supplied SQL but enforces a strict allowlist: only `SELECT` and `WITH` are permitted at the statement level. `DROP`, `DELETE`, `INSERT`, `UPDATE`, and `PRAGMA` all raise an error before touching the database.

### 5. Cost tracking at every query

A `UsageTracker` LangChain callback accumulates input/output/cache tokens across all LLM calls in a run (supervisor + all subagent calls). On completion it computes USD cost and writes a row to `agent_usage`. Running cost across all sessions is queryable in seconds.

---

## Tool inventory

| Category | Tool | What it does |
|----------|------|--------------|
| **System** | `check_dibo_reachable` | SSH connectivity health check |
| | `get_disk_usage` | All mounted filesystems (excl. tmpfs/overlay) |
| | `get_memory_stats` | RAM and swap |
| | `get_load_average` | CPU load + uptime |
| | `get_service_status` | Systemd unit status |
| | `get_journal_logs` | Filtered journald logs |
| | `get_top_processes` | Top-N by CPU or memory |
| **Docker** | `list_containers` | Fleet with state/status |
| | `get_container_stats` | CPU, memory, network I/O |
| | `get_container_logs` | Container stdout/stderr |
| | `inspect_container` | Restart count, health, mounts |
| | `find_recently_restarted_containers` | Flapping detection |
| **AdGuard** | `get_adguard_status` | Version, protection state, DNS port |
| | `get_adguard_stats` | Rolling 24h query/block counts |
| | `get_adguard_query_log` | Recent DNS queries with filter |
| | `get_adguard_top_blocked` | Most-blocked domains |
| | `get_adguard_top_clients` | Most-active DNS clients |
| **History** | `query_history` | Read-only SQL on DuckDB metrics store |
| | `get_snapshot_coverage` | Data span check before temporal claims |
| **Write** | `restart_container` | Docker restart — requires approval |
| | `flush_adguard_cache` | Clear DNS cache — requires approval |
| | `reboot_dibo` | Full server reboot — requires approval |

---

## Metrics pipeline

A systemd timer on dibo runs `python -m homelab_agent.ingest.snapshot` every 5 minutes. Each invocation opens a new snapshot (keyed by Unix epoch), collects all metric types in parallel try/except blocks (one failure doesn't abort the snapshot), and commits to DuckDB.

**Schema:** `snapshots`, `disk_usage`, `memory_stats`, `load_average`, `container_stats`, `adguard_stats`, `agent_usage`, `action_proposals`

The timer uses `Persistent=true` — if dibo reboots mid-cycle, missed runs are caught up automatically.

---

## Real incident: 15h41m overnight outage (2026-05-14)

On the night of 13–14 May 2026, dibo was taken offline for a kernel upgrade and reboot. The upgrade ran longer than expected; the server was down for 15 hours and 41 minutes.

The agent diagnosed this from the snapshot gap alone:

> *"The last snapshot before the gap was at 22:18 UTC on May 13. The next snapshot is at 13:59 UTC on May 14 — a gap of 15 hours and 41 minutes. This is consistent with a planned maintenance window or an unexpected outage. All metrics resumed normally after the gap; no data was lost."*

See [`incidents/2026-05-14-overnight-outage.md`](incidents/2026-05-14-overnight-outage.md) for the full agent trace.

---

## Costs

Measured on Claude Sonnet 4.5 (May 2026 pricing: $3/M input, $15/M output):

| Query type | LLM calls | Approx. cost |
|------------|-----------|--------------|
| Single-domain question | 2–4 | ~$0.01–0.05 |
| Cross-cutting health check | 10–14 | ~$0.20–0.40 |
| HITL write action (approve path) | 2 | ~$0.009 |

All costs are persisted to `agent_usage` and queryable:

```bash
python3 -c "
from homelab_agent.ingest.schema import get_connection
conn = get_connection()
total = conn.execute('SELECT SUM(estimated_cost_usd) FROM agent_usage').fetchone()[0] or 0
print(f'Total spent: \${total:.4f}')
"
```

---

## Running it

```bash
# Prerequisites: Python 3.12, venv, .env file (see .env.example)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Single-agent (19 tools, hardcoded test questions)
python -m homelab_agent.agent

# Multi-agent supervisor with HITL
python -m homelab_agent.multi_agent

# One-shot metrics snapshot
python -m homelab_agent.ingest.snapshot

# Integration tests (requires live dibo)
pytest tests/ -v
```

```bash
# Run on dibo against its live metrics DB
ssh <user>@dibo 'cd homelab-agent && .venv/bin/python -m homelab_agent.multi_agent'
```

---

## Tests

69 integration tests across three suites (`system_tools`, `docker_tools`, `adguard_tools`). All tests hit live infrastructure — no mocked SSH or database. A session-scoped fixture skips the suite if dibo is unreachable.

```bash
pytest tests/ -v  # ~15s, requires dibo on Tailscale
```

---

## What I'd do with more time

- **Eval harness** — 25-question ground-truth set, LLM-as-judge scoring, latency/cost/accuracy metrics across model variants
- **Local model benchmark** — run the eval against a quantized 7B (Qwen 2.5 / Llama 3.1) via Ollama on an RTX 5070 Ti, compare against Sonnet 4.5 on accuracy and cost
- **Streamlit UI** — chat interface, trace panel showing every tool call, pending-approvals queue for HITL actions
- **More write tools** — `kill_torrent`, `enable_adguard_protection`, `set_download_limit`; each with the same interrupt/audit pattern
- **Persistent checkpoints** — swap `InMemorySaver` for `SqliteSaver` so interrupted write actions survive process restarts
- **Alert hooks** — cron job that queries the agent ("is anything degraded?") and sends a push notification if the answer contains a warning
