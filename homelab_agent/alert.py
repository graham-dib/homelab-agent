"""Proactive health alert: run the agent, push Telegram messages when warranted.

Behaviour:
- Every run (every 30 min via systemd timer):
    • Sends an instant Telegram warning if the answer contains problem signals.
- Daily summary:
    • The first run on or after DAILY_SUMMARY_HOUR UTC sends a digest covering
      how many checks ran today, how many were warnings, and the current state.

State is persisted in alert_state.json next to this file so the daily summary
logic survives process restarts.

Usage:
    python -m homelab_agent.alert            # normal run
    python -m homelab_agent.alert --dry-run  # print without sending

Deploy:
    sudo cp deploy/dibo-alert.{service,timer} /etc/systemd/system/
    sudo systemctl enable --now dibo-alert.timer
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, date
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

from homelab_agent.config import settings

HEALTH_QUESTION = (
    "Give me a brief health summary of dibo right now. "
    "Check disk usage, memory, CPU load, and whether all key containers (plex, "
    "adguard, transmission, omada) are running. "
    "Flag anything that looks degraded, elevated, or unusual. "
    "If everything is normal, say so explicitly in one line."
)

WARNING_PATTERNS = [
    r"\bdegraded\b",
    r"\bunhealthy\b",
    r"\bfailed?\b",
    r"\bdown\b(?!\s*to\b)",       # "down" but not "down to X%"
    r"\bstopped\b",
    r"\b(?:8[5-9]|9[0-9])%",        # 85%+ usage
    r"\b(disk|storage|memory|ram)\b.{0,50}\b(full|critical|warn|near.?full)\b",
    r"\bhigh\b.{0,30}\b(load|cpu|memory|disk)\b",
    r"\brestarted?\s+\d+\s+times?\b",
    r"\berror\b",
    r"\bcaution\b",
    r"\baction\s+recommended\b",
    r"\bapproaching\s+threshold\b",
    r"\belevated\b",
]
WARNING_RE = re.compile("|".join(WARNING_PATTERNS), re.IGNORECASE)

DAILY_SUMMARY_HOUR = 8   # London local hour to send the daily digest
LONDON_TZ = ZoneInfo("Europe/London")

STATE_PATH = Path(__file__).resolve().parent.parent / "alert_state.json"


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {
        "last_summary_date": None,
        "checks_today": 0,
        "warnings_today": 0,
    }


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"[ALERT] Could not save state: {e}", file=sys.stderr)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send_telegram(text: str, dry_run: bool = False) -> None:
    token = getattr(settings, "telegram_bot_token", "")
    chat_id = getattr(settings, "telegram_chat_id", "")

    if dry_run:
        print(f"[DRY RUN] Would send to Telegram chat {chat_id or '<not set>'}:")
        print("─" * 60)
        print(text)
        print("─" * 60)
        return

    if not token or not chat_id:
        print("[ALERT] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping.", file=sys.stderr)
        return

    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        ).raise_for_status()
        print("[ALERT] Telegram message sent.")
    except Exception as e:
        print(f"[ALERT] Telegram send failed: {e}", file=sys.stderr)


# ── Agent run ─────────────────────────────────────────────────────────────────

def _run_health_check() -> tuple[str, float]:
    """Invoke the single-agent health check. Returns (answer, cost_usd)."""
    from homelab_agent.agent import build_agent
    from homelab_agent.cost_tracking import UsageTracker

    agent = build_agent()
    tracker = UsageTracker(HEALTH_QUESTION)
    try:
        result = agent.invoke(
            {"messages": [("user", HEALTH_QUESTION)]},
            config={
                "configurable": {"thread_id": str(uuid4())},
                "callbacks": [tracker],
            },
        )
        messages = result.get("messages", [])
        for msg in reversed(messages):
            if msg.type != "ai" or msg.tool_calls:
                continue
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    b["text"] for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if content and content.strip():
                usage = tracker.persist()
                return content.strip(), usage.get("estimated_cost_usd", 0.0)
    except Exception as e:
        tracker.persist()
        return f"[Agent error: {type(e).__name__}: {e}]", 0.0

    tracker.persist()
    return "(no answer returned)", 0.0


# ── Message formatting ────────────────────────────────────────────────────────

def _warning_message(answer: str, now: datetime) -> str:
    ts = now.strftime("%H:%M UTC")
    # Truncate to Telegram's 4096-char limit with room for the header
    body = answer[:3800]
    return (
        f"⚠️ <b>dibo — health warning</b> ({ts})\n\n"
        f"{body}"
    )


def _summary_message(answer: str, checks: int, warnings: int, now: datetime) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    status = "⚠️ warnings detected" if warnings else "✅ all checks passed"
    body = answer[:3200]
    return (
        f"📊 <b>dibo — daily summary</b> ({ts})\n\n"
        f"Checks today: {checks}  |  Warnings: {warnings}  →  {status}\n\n"
        f"<b>Current state:</b>\n{body}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run_alert(dry_run: bool = False) -> int:
    now = datetime.now(timezone.utc)
    now_london = now.astimezone(LONDON_TZ)
    today_str = now_london.date().isoformat()  # "today" in London time

    state = _load_state()

    # Reset daily counters if it's a new day (London calendar date)
    if state.get("last_summary_date") != today_str:
        state["checks_today"] = 0
        state["warnings_today"] = 0

    print(f"[ALERT] Running health check at {now.strftime('%H:%M UTC')}…")
    answer, cost = _run_health_check()
    print(f"[ALERT] Agent answered (${cost:.4f}):")
    print(answer)
    print()

    has_warning = bool(WARNING_RE.search(answer))
    state["checks_today"] = state.get("checks_today", 0) + 1
    if has_warning:
        state["warnings_today"] = state.get("warnings_today", 0) + 1

    sent_something = False

    # ── Instant warning ───────────────────────────────────────────────────────
    if has_warning:
        print("[ALERT] Warning patterns detected → sending Telegram alert.")
        _send_telegram(_warning_message(answer, now), dry_run=dry_run)
        sent_something = True

    # ── Daily summary ─────────────────────────────────────────────────────────
    last_summary = state.get("last_summary_date")
    due_for_summary = (
        now_london.hour >= DAILY_SUMMARY_HOUR
        and last_summary != today_str
    )
    if due_for_summary:
        print("[ALERT] Daily summary due → sending.")
        _send_telegram(
            _summary_message(answer, state["checks_today"], state["warnings_today"], now),
            dry_run=dry_run,
        )
        state["last_summary_date"] = today_str
        sent_something = True

    if not sent_something:
        print("[ALERT] No warning, daily summary not yet due — no message sent.")

    _save_state(state)
    return 2 if has_warning else 0   # exit 2 = warning (useful for monitoring wrappers)


def main() -> None:
    parser = argparse.ArgumentParser(description="dibo health alert")
    parser.add_argument("--dry-run", action="store_true", help="Print without sending")
    args = parser.parse_args()
    sys.exit(run_alert(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
