# Incident: 15h41m overnight outage — 2026-05-14

**Duration:** 15h 41m 27s  
**Outage window:** 2026-05-13 18:43 UTC → 2026-05-14 10:25 UTC  
**Cause:** Planned kernel upgrade and reboot  
**Severity:** Full service outage (all containers offline)  
**Detected by:** Agent gap analysis on snapshot history

---

## What happened

dibo was taken offline on the evening of 13 May for a kernel upgrade. The upgrade and reboot sequence ran through the night; the server came back online at 10:25 UTC on 14 May. The systemd snapshot timer (using `Persistent=true`) resumed automatically on boot and caught up any missed runs.

No data was lost. The metrics gap is expected and correctly attributed.

---

## Agent diagnosis

The outage was diagnosed entirely from the snapshot gap — no manual investigation was needed.

**Q1: "How much data have we collected so far? What time range does it cover?"**

> The metrics database contains 1,042 snapshots spanning 4.4 days (2026-05-13 15:19 UTC to 2026-05-18 01:42 UTC). Before answering any trend questions, I called `get_snapshot_coverage` to confirm the data span.

**Q2: "Was there any disruption to dibo over the collection period? What does the data show?"**

> There is one significant gap in the snapshot history: the last snapshot before the gap was recorded at **2026-05-13 18:43:56 UTC**, and the next was at **2026-05-14 10:25:23 UTC** — a gap of **56,487 seconds (15 hours and 41 minutes)**. This is the only gap exceeding 10 minutes in the entire dataset.
>
> The gap was identified with this query:
> ```sql
> SELECT
>     timestamp,
>     prev_ts,
>     ROUND((epoch(timestamp) - epoch(prev_ts)) / 3600.0, 2) AS gap_hours
> FROM (
>     SELECT
>         timestamp,
>         LAG(timestamp) OVER (ORDER BY timestamp) AS prev_ts
>     FROM snapshots
> ) sub
> WHERE prev_ts IS NOT NULL
>   AND epoch(timestamp) - epoch(prev_ts) > 600
> ORDER BY gap_hours DESC
> ```
>
> Metrics immediately before and after the gap:
>
> | Metric | Pre-outage (18:43) | Post-reboot (10:25) | Interpretation |
> |--------|--------------------|---------------------|----------------|
> | `/srv/storage` usage | 86% | 86% | No change — disk stable |
> | Memory usage | 21.7% | 18.0% | Normal post-reboot drop (cold start) |
> | CPU load (1min) | 0.01 | 0.68 | Slight boot-up spike, normalised within 5 min |
>
> All metrics returned to normal baseline within the first two post-reboot snapshots. There is no evidence of filesystem corruption, data loss, or service instability after the outage. The snapshot timer resumed without manual intervention.

---

## What this demonstrates

- **Temporal awareness:** The agent called `get_snapshot_coverage` before answering any questions about trends, ensuring it didn't make claims beyond the data span.
- **Gap detection via SQL:** A LAG window function over the snapshots table reliably identifies outage windows without any special alerting infrastructure.
- **Honest refusal:** For the first run (1.7 hours of data), the agent refused to claim AdGuard DNS trends: *"Without at least a few snapshots over time, I can't tell you whether query counts are trending up, down, or stable."*
- **Continuity:** The `Persistent=true` systemd timer flag ensured the pipeline resumed automatically — no manual restart needed after 15h41m offline.
