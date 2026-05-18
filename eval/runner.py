"""Eval harness runner for dibo-agent.

Runs a question set against the live agent and stores results in DuckDB.

Usage:
    python -m eval.runner                         # all 25 questions, multi-agent
    python -m eval.runner --agent single          # single-agent mode
    python -m eval.runner --filter fast           # only questions tagged 'fast'
    python -m eval.runner --ids Q01,Q03,Q25       # specific question IDs
    python -m eval.runner --dry-run               # print questions, don't run agent
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from dotenv import load_dotenv

load_dotenv()

from homelab_agent.ingest.schema import get_connection
from homelab_agent.tools.system_tools import check_dibo_reachable
from homelab_agent.cost_tracking import UsageTracker
from eval.scoring import score_result

QUESTIONS_FILE = Path(__file__).parent / "questions.yaml"


# ── LLM construction ──────────────────────────────────────────────────────────

def _build_llm(model_name: str, ollama_host: str = "localhost:11434"):
    """Return a LangChain chat model. Anthropic if name starts with 'claude', else Ollama."""
    if model_name.startswith("claude"):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model_name, temperature=0)
    else:
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model_name,
            base_url=f"http://{ollama_host}",
            temperature=0,
        )


# ── Agent construction ────────────────────────────────────────────────────────

def _build_agent(mode: str, llm):
    if mode == "single":
        from homelab_agent.agent import build_agent
        return build_agent(llm=llm), "single"
    else:
        from homelab_agent.multi_agent import build_supervisor
        return build_supervisor(llm=llm), "multi"


def _extract_answer(result: dict) -> str:
    """Pull the final AI text response from an agent invoke result."""
    messages = result.get("messages", [])
    # Walk backwards — last AI message with text content is the final answer
    for msg in reversed(messages):
        if msg.type != "ai":
            continue
        if msg.tool_calls:
            continue
        content = msg.content
        if isinstance(content, list):
            content = " ".join(
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if content and content.strip():
            return content.strip()
    return ""


# ── Storage ───────────────────────────────────────────────────────────────────

def _store_results(rows: list[dict]) -> None:
    conn = get_connection()
    try:
        for row in rows:
            conn.execute(
                """INSERT OR REPLACE INTO eval_results (
                    run_id, question_id, category, question, agent_answer, model,
                    check_type, programmatic_pass, programmatic_detail,
                    judge_score, judge_reason, judge_model, judge_cost_usd,
                    composite_score, n_llm_calls, input_tokens, output_tokens,
                    cache_read_tokens, agent_cost_usd, latency_seconds, error, timestamp
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    row["run_id"], row["question_id"], row["category"],
                    row["question"], row["agent_answer"], row["model"],
                    row["check_type"], row["programmatic_pass"],
                    row["programmatic_detail"], row["judge_score"],
                    row["judge_reason"], row["judge_model"], row["judge_cost_usd"],
                    row["composite_score"], row["n_llm_calls"], row["input_tokens"],
                    row["output_tokens"], row["cache_read_tokens"],
                    row["agent_cost_usd"], row["latency_seconds"],
                    row["error"], row["timestamp"],
                ],
            )
        conn.commit()
    finally:
        conn.close()


# ── Progress printing ─────────────────────────────────────────────────────────

def _print_row(q_id: str, category: str, scores: dict, usage: dict, latency: float) -> None:
    prog = scores["programmatic_pass"]
    prog_str = "✓" if prog is True else ("✗" if prog is False else "–")
    judge = scores["judge_score"]
    judge_str = f"{judge}/5" if judge > 0 else "err"
    composite = scores["composite_score"]
    cost = usage.get("estimated_cost_usd", 0)
    print(
        f"  {q_id:<5} [{category:<14}] "
        f"prog={prog_str}  judge={judge_str}  composite={composite:.2f}  "
        f"${cost:.4f}  {latency:.1f}s"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Run dibo-agent eval harness")
    parser.add_argument("--agent", choices=["single", "multi"], default="multi")
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        metavar="MODEL",
        help="LLM to benchmark. 'claude-*' → Anthropic API; anything else → Ollama. "
             "Examples: claude-sonnet-4-5-20250929, qwen2.5:14b, llama3.1:latest",
    )
    parser.add_argument(
        "--ollama-host",
        default="localhost:11434",
        metavar="HOST:PORT",
        help="Ollama API host (default: localhost:11434). Set to a remote host when "
             "running on a GPU-less machine (e.g. 100.90.119.31:11435 via Tailscale).",
    )
    parser.add_argument("--filter", metavar="TAG", help="Only run questions with this tag")
    parser.add_argument("--ids", metavar="Q01,Q02", help="Comma-separated question IDs to run")
    parser.add_argument("--dry-run", action="store_true", help="Print questions without running")
    args = parser.parse_args()

    # Load questions
    with open(QUESTIONS_FILE) as f:
        questions = yaml.safe_load(f)

    # Filter
    if args.ids:
        wanted = {q.strip() for q in args.ids.split(",")}
        questions = [q for q in questions if q["id"] in wanted]
    elif args.filter:
        questions = [q for q in questions if args.filter in q.get("tags", [])]

    if not questions:
        print("No questions match the filter. Exiting.")
        sys.exit(1)

    if args.dry_run:
        print(f"DRY RUN — {len(questions)} questions:")
        for q in questions:
            print(f"  {q['id']}: {q['question'][:80]}")
        return

    # Connectivity gate
    print("Checking dibo connectivity...", end=" ", flush=True)
    reach = check_dibo_reachable.invoke({})
    if not reach.get("reachable"):
        print("FAIL — dibo is not reachable. Aborting.")
        sys.exit(1)
    print(f"OK ({reach.get('hostname', 'dibo')})")

    # Build LLM and agent once
    print(f"Building agent (mode={args.agent}, model={args.model})...", end=" ", flush=True)
    llm = _build_llm(args.model, ollama_host=args.ollama_host)
    agent, mode = _build_agent(args.agent, llm)
    print("OK")

    run_id = str(uuid4())
    print(f"\nEval run {run_id[:8]} — {len(questions)} questions — agent={mode}")
    print("-" * 70)

    results = []
    total_agent_cost = 0.0
    total_judge_cost = 0.0
    passed = 0
    judge_total = 0
    judge_count = 0

    for q in questions:
        tracker = UsageTracker(f"[EVAL:{run_id[:8]}] {q['question']}")
        t0 = time.time()
        answer = ""
        error = None
        model = None

        try:
            result = agent.invoke(
                {"messages": [("user", q["question"])]},
                config={"callbacks": [tracker]},
            )
            answer = _extract_answer(result)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        latency = time.time() - t0
        usage = tracker.persist()
        # For Ollama models the callback may not capture model name; fall back to CLI arg
        model = usage.get("model") or args.model

        scores = score_result(q, answer)
        _print_row(q["id"], q["category"], scores, usage, latency)

        if scores["programmatic_pass"] is True:
            passed += 1
        if scores["judge_score"] > 0:
            judge_total += scores["judge_score"]
            judge_count += 1

        total_agent_cost += usage.get("estimated_cost_usd", 0)
        total_judge_cost += scores["judge_cost_usd"]

        results.append({
            "run_id": run_id,
            "question_id": q["id"],
            "category": q.get("category", ""),
            "question": q["question"],
            "agent_answer": answer[:5000],
            "model": model,
            **scores,
            "n_llm_calls": usage.get("n_llm_calls", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "agent_cost_usd": usage.get("estimated_cost_usd", 0),
            "latency_seconds": round(latency, 2),
            "error": error,
            "timestamp": datetime.now(timezone.utc),
        })

    _store_results(results)

    n = len(questions)
    prog_eligible = sum(1 for r in results if r["programmatic_pass"] is not None)
    mean_judge = (judge_total / judge_count) if judge_count else 0
    mean_composite = sum(r["composite_score"] for r in results) / n

    print("-" * 70)
    print(f"Run {run_id[:8]} complete")
    print(f"  Programmatic pass : {passed}/{prog_eligible}")
    print(f"  Mean judge score  : {mean_judge:.2f}/5.0  ({judge_count} judged)")
    print(f"  Mean composite    : {mean_composite:.2f}")
    print(f"  Agent cost        : ${total_agent_cost:.4f}")
    print(f"  Judge cost        : ${total_judge_cost:.4f}")
    print(f"  Total cost        : ${total_agent_cost + total_judge_cost:.4f}")
    print(f"  Results stored    : eval_results table (run_id={run_id[:8]})")


if __name__ == "__main__":
    main()
