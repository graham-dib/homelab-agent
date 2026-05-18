"""Print eval result summaries from the DuckDB store.

Usage:
    python -m eval.report                         # latest run
    python -m eval.report --run-id a3f2b1c0       # specific run (prefix OK)
    python -m eval.report --list                   # list all runs
    python -m eval.report --compare a3f2 d1e4      # side-by-side two runs
"""
from __future__ import annotations

import argparse
import sys
from dotenv import load_dotenv

load_dotenv()

from homelab_agent.ingest.schema import get_connection


def _resolve_run_id(conn, prefix: str) -> str | None:
    rows = conn.execute(
        "SELECT run_id FROM eval_results GROUP BY run_id ORDER BY MIN(timestamp) DESC"
    ).fetchall()
    matches = [r[0] for r in rows if r[0].startswith(prefix)]
    return matches[0] if matches else None


def _print_run(conn, run_id: str) -> None:
    rows = conn.execute(
        """SELECT question_id, category, programmatic_pass, judge_score,
                  composite_score, agent_cost_usd, judge_cost_usd, latency_seconds,
                  model, error
           FROM eval_results
           WHERE run_id = ?
           ORDER BY question_id""",
        [run_id],
    ).fetchall()

    if not rows:
        print(f"No results for run {run_id[:8]}")
        return

    model = rows[0][8] or "unknown"
    n = len(rows)
    passed = sum(1 for r in rows if r[2] is True)
    prog_eligible = sum(1 for r in rows if r[2] is not None)
    judge_scores = [r[3] for r in rows if r[3] and r[3] > 0]
    mean_judge = sum(judge_scores) / len(judge_scores) if judge_scores else 0
    mean_composite = sum(r[4] for r in rows) / n
    agent_cost = sum(r[5] or 0 for r in rows)
    judge_cost = sum(r[6] or 0 for r in rows)

    print(f"\nEval run: {run_id[:8]}  model: {model}  questions: {n}")
    print(f"{'─' * 72}")
    print(f"{'ID':<6} {'Category':<16} {'Prog':>4} {'Judge':>5} {'Comp':>5} {'Cost':>7} {'Lat':>6}")
    print(f"{'─' * 72}")

    for q_id, cat, prog, judge, comp, acost, jcost, lat, _, err in rows:
        prog_s = "✓" if prog is True else ("✗" if prog is False else " –")
        judge_s = f"{judge}/5" if judge and judge > 0 else (" err" if judge == -1 else "  –")
        comp_s = f"{comp:.2f}" if comp is not None else "   –"
        cost_s = f"${(acost or 0) + (jcost or 0):.4f}"
        lat_s = f"{lat:.1f}s" if lat else "   –"
        err_flag = " !" if err else ""
        print(f"{q_id:<6} {cat:<16} {prog_s:>4} {judge_s:>5} {comp_s:>5} {cost_s:>7} {lat_s:>6}{err_flag}")

    print(f"{'─' * 72}")
    print(f"{'TOTAL':<6} {'':16} {passed}/{prog_eligible:>2} {mean_judge:>4.1f}/5 {mean_composite:>5.2f} ${agent_cost + judge_cost:.4f}")
    print()


def _list_runs(conn) -> None:
    rows = conn.execute(
        """SELECT run_id, COUNT(*) AS n, MIN(timestamp) AS started,
                  SUM(agent_cost_usd + judge_cost_usd) AS total_cost,
                  AVG(composite_score) AS mean_composite,
                  MIN(model) AS model
           FROM eval_results
           GROUP BY run_id
           ORDER BY started DESC""",
    ).fetchall()

    if not rows:
        print("No eval runs found.")
        return

    print(f"\n{'Run ID':<10} {'Date':<22} {'Qs':>3} {'Mean':>5} {'Cost':>7} {'Model'}")
    print("─" * 65)
    for run_id, n, started, cost, composite, model in rows:
        started_s = str(started)[:16] if started else "–"
        composite_s = f"{composite:.2f}" if composite else "–"
        cost_s = f"${cost:.4f}" if cost else "–"
        print(f"{run_id[:8]:<10} {started_s:<22} {n:>3} {composite_s:>5} {cost_s:>7} {model or '–'}")
    print()


def _compare_runs(conn, run_a: str, run_b: str) -> None:
    def fetch(run_id):
        return {
            r[0]: r for r in conn.execute(
                "SELECT question_id, composite_score, judge_score FROM eval_results WHERE run_id = ? ORDER BY question_id",
                [run_id],
            ).fetchall()
        }

    a_rows = fetch(run_a)
    b_rows = fetch(run_b)
    ids = sorted(set(a_rows) | set(b_rows))

    print(f"\nCompare {run_a[:8]} vs {run_b[:8]}")
    print(f"{'ID':<6} {'A comp':>7} {'B comp':>7} {'Δ':>6} {'A judge':>7} {'B judge':>7}")
    print("─" * 50)

    for q_id in ids:
        a = a_rows.get(q_id)
        b = b_rows.get(q_id)
        a_comp = a[1] if a else None
        b_comp = b[1] if b else None
        delta = (b_comp - a_comp) if (a_comp is not None and b_comp is not None) else None
        a_j = f"{a[2]}/5" if a and a[2] and a[2] > 0 else "–"
        b_j = f"{b[2]}/5" if b and b[2] and b[2] > 0 else "–"
        delta_s = f"{delta:+.2f}" if delta is not None else "–"
        a_cs = f"{a_comp:.2f}" if a_comp is not None else "–"
        b_cs = f"{b_comp:.2f}" if b_comp is not None else "–"
        print(f"{q_id:<6} {a_cs:>7} {b_cs:>7} {delta_s:>6} {a_j:>7} {b_j:>7}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Report eval results")
    parser.add_argument("--run-id", metavar="PREFIX", help="Show a specific run (8-char prefix OK)")
    parser.add_argument("--list", action="store_true", help="List all runs")
    parser.add_argument("--compare", nargs=2, metavar="RUN", help="Compare two run IDs")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.list:
            _list_runs(conn)
        elif args.compare:
            a = _resolve_run_id(conn, args.compare[0])
            b = _resolve_run_id(conn, args.compare[1])
            if not a or not b:
                print("One or both run IDs not found.")
                sys.exit(1)
            _compare_runs(conn, a, b)
        else:
            run_id = args.run_id or ""
            resolved = _resolve_run_id(conn, run_id)
            if not resolved:
                print("No runs found." if not run_id else f"Run {run_id!r} not found.")
                sys.exit(1)
            _print_run(conn, resolved)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
