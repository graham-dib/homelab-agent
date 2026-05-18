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
| | `kill_torrent` | Stop (pause) a Transmission torrent — requires approval |
| | `enable_adguard_protection` | Toggle AdGuard DNS filtering on/off — requires approval |
| | `set_download_limit` | Cap Transmission global download speed — requires approval |

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

## Model benchmark

The eval harness (25 questions, two-phase scoring) was run against both Claude Sonnet 4.5 and a local qwen2.5:14b (Q4_K_M, ~9 GB VRAM) served by Ollama on an RTX 5070 Ti. Both runs executed **on dibo** against its live metrics DB, with Ollama API calls forwarded over Tailscale to the desktop GPU.

| Model | Prog pass | Mean judge | Composite | Latency (avg) | Agent cost |
|-------|-----------|------------|-----------|---------------|------------|
| Claude Sonnet 4.5 | **13/15** | **4.25/5** | **0.77** | 21s | $1.12 |
| qwen2.5:14b (local) | 11/15 | 3.68/5 | 0.66 | 7s | **$0.00** |

**Where Sonnet 4.5 wins clearly:** detailed analysis questions — container log parsing (Q13: +0.80), AdGuard query log & top-clients (Q16, Q18: +0.80), container health inspection (Q12, Q13). These require extracting precise values from multi-tool reasoning chains.

**Where qwen2.5:14b surprises:** temporal/history questions (Q20–Q22, Δ –0.20 to –0.60 in qwen's favour) — the 14B model produces more direct DuckDB `SELECT` calls without overthinking. Sonnet spent $0.13 on Q20 making many tool attempts; qwen answered the same question correctly in 5s.

**Tie or near-tie (Δ ≤ 0.07):** 14 of 25 questions — simple diagnostics (Q01–Q08), AdGuard status (Q14, Q15, Q17), snapshot coverage (Q19), overnight-gap diagnosis (Q25). For routine monitoring qwen2.5:14b is equivalent at zero marginal cost.

**Overall:** ~14% composite score gap, 3× faster, 100% cheaper on agent inference. The cost-quality frontier is well-defined: use Sonnet 4.5 when precision matters on complex multi-tool chains; use a local model for routine health checks.

```bash
# Run the benchmark yourself (from dibo, with Ollama on a remote GPU)
python -m eval.runner --agent single --model claude-sonnet-4-5-20250929
python -m eval.runner --agent single --model qwen2.5:14b --ollama-host <desktop-tailscale-ip>:11435
python -m eval.report --compare <run_a> <run_b>
```

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

# Streamlit UI
streamlit run app.py   # http://localhost:8501

# Alert dry-run (prints health summary, shows what would be pushed)
python -m homelab_agent.alert --dry-run
```

```bash
# Deploy alert timer on dibo (runs every 30 min, pushes to ntfy.sh)
# 1. Add NTFY_TOPIC=your-topic to dibo's .env
# 2. Copy the units and enable:
sudo cp deploy/dibo-alert.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dibo-alert.timer
```

---

## Tests

69 integration tests across three suites (`system_tools`, `docker_tools`, `adguard_tools`). All tests hit live infrastructure — no mocked SSH or database. A session-scoped fixture skips the suite if dibo is unreachable.

```bash
pytest tests/ -v  # ~15s, requires dibo on Tailscale
```

---

## What I'd do with more time

- **Persistent checkpoints** — swap `InMemorySaver` for `SqliteSaver` so interrupted write actions survive process restarts
- **Loom walkthrough** — 3-minute demo showing agent diagnosing a real issue end-to-end
