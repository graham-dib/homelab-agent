"""Two-way Telegram bot — polls for messages and routes them to the homelab agent.

Only responds to the chat ID set in TELEGRAM_CHAT_ID (your personal chat).
Messages from any other chat are silently ignored.

Deletion approval flow:
  When the agent proposes files for deletion it includes a DELETION_CANDIDATES
  block. The bot strips this from the visible reply, stores the paths, and asks
  for yes/no confirmation. On "yes" it SSHes to dibo and removes the files
  directly. On anything else it cancels.

Deploy:
    sudo cp deploy/dibo-telegram.service /etc/systemd/system/
    sudo systemctl enable --now dibo-telegram.service
"""
from __future__ import annotations

import re
import shlex
import sys
import time
from uuid import uuid4

import httpx
from dotenv import load_dotenv

load_dotenv()

from homelab_agent.config import settings

POLL_TIMEOUT = 30   # long-poll seconds per getUpdates call

_DELETION_RE = re.compile(
    r"DELETION_CANDIDATES:\n(.*?)\nEND_DELETION_CANDIDATES",
    re.DOTALL,
)

# chat_id → list of /srv/ paths awaiting yes/no confirmation
_pending_deletions: dict[int, list[str]] = {}

# chat_id → list of (role, content) message pairs, capped at MAX_HISTORY
MAX_HISTORY = 10
_chat_histories: dict[int, list[tuple[str, str]]] = {}


# ── Telegram API ──────────────────────────────────────────────────────────────

def _api(path: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{path}"


def _get_updates(offset: int) -> list[dict]:
    try:
        resp = httpx.get(
            _api("getUpdates"),
            params={
                "offset": offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message"],
            },
            timeout=POLL_TIMEOUT + 5.0,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[BOT] getUpdates error: {e}", file=sys.stderr)
        time.sleep(5)
        return []


def _send(chat_id: int, text: str) -> None:
    """Send a plain-text reply, splitting at 4096 chars if needed."""
    for chunk in [text[i : i + 4096] for i in range(0, len(text), 4096)]:
        try:
            httpx.post(
                _api("sendMessage"),
                json={"chat_id": chat_id, "text": chunk},
                timeout=10.0,
            ).raise_for_status()
        except Exception as e:
            print(f"[BOT] sendMessage error: {e}", file=sys.stderr)


def _typing(chat_id: int) -> None:
    try:
        httpx.post(
            _api("sendChatAction"),
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5.0,
        )
    except Exception:
        pass


# ── Deletion flow ─────────────────────────────────────────────────────────────

def _extract_deletion_proposal(text: str) -> tuple[str, list[str]]:
    """Strip the DELETION_CANDIDATES block from the agent reply.

    Returns (display_text, paths). paths is empty if no block found.
    """
    m = _DELETION_RE.search(text)
    if not m:
        return text, []
    paths = [
        p.strip() for p in m.group(1).strip().splitlines()
        if p.strip().startswith("/srv/")
    ]
    display = _DELETION_RE.sub("", text).strip()
    return display, paths


def _execute_deletions(chat_id: int, paths: list[str]) -> None:
    from homelab_agent.tools._clients import run_on_dibo

    bad = [p for p in paths if not p.startswith("/srv/")]
    if bad:
        _send(chat_id, "Refused — paths outside /srv/ are not allowed:\n" + "\n".join(bad))
        return
    try:
        quoted = " ".join(shlex.quote(p) for p in paths)
        total = run_on_dibo(
            f"du -shc {quoted} 2>/dev/null | tail -1 | awk '{{print $1}}'",
            timeout=20,
        ) or "unknown"
        run_on_dibo(f"rm -f {quoted}", timeout=60)
        _send(chat_id, f"Deleted {len(paths)} file(s) (~{total} freed).")
        print(f"[BOT] Deleted {len(paths)} files, ~{total} freed.")
    except Exception as e:
        _send(chat_id, f"Deletion failed: {e}")
        print(f"[BOT] Deletion error: {e}", file=sys.stderr)


def _handle_pending_approval(chat_id: int, text: str) -> bool:
    """If approval is pending for this chat, handle it. Returns True if handled."""
    if chat_id not in _pending_deletions:
        return False
    paths = _pending_deletions.pop(chat_id)
    if text.lower().strip() in ("yes", "y", "approve", "confirm", "delete"):
        _send(chat_id, f"Deleting {len(paths)} file(s)...")
        _execute_deletions(chat_id, paths)
    else:
        _send(chat_id, "Deletion cancelled. Nothing was removed.")
    return True


# ── Agent ─────────────────────────────────────────────────────────────────────

def _run_agent(chat_id: int, question: str) -> str:
    from homelab_agent.agent import build_agent
    from homelab_agent.cost_tracking import UsageTracker

    history = _chat_histories.get(chat_id, [])
    messages = history + [("user", question)]

    agent = build_agent()
    tracker = UsageTracker(question)
    try:
        result = agent.invoke(
            {"messages": messages},
            config={
                "configurable": {"thread_id": str(uuid4())},
                "callbacks": [tracker],
            },
        )
        for msg in reversed(result.get("messages", [])):
            if msg.type != "ai" or msg.tool_calls:
                continue
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    b["text"] for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if content and content.strip():
                tracker.persist()
                answer = content.strip()
                # Update history, keeping last MAX_HISTORY messages
                updated = history + [("user", question), ("assistant", answer)]
                _chat_histories[chat_id] = updated[-MAX_HISTORY:]
                return answer
    except Exception as e:
        tracker.persist()
        return f"[Agent error: {type(e).__name__}: {e}]"

    tracker.persist()
    return "(no answer returned)"


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_bot() -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        print("[BOT] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.", file=sys.stderr)
        sys.exit(1)

    allowed_chat_id = int(settings.telegram_chat_id)
    print(f"[BOT] Listening for messages from chat {allowed_chat_id}…")

    offset = 0
    while True:
        updates = _get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1

            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()

            if not text or not chat_id:
                continue

            if chat_id != allowed_chat_id:
                print(f"[BOT] Ignoring message from unauthorised chat {chat_id}")
                continue

            print(f"[BOT] Q: {text!r}")

            # Check for pending deletion approval before running the agent
            if _handle_pending_approval(chat_id, text):
                continue

            _typing(chat_id)
            answer = _run_agent(chat_id, text)

            # Extract any deletion proposal from the response
            display, paths = _extract_deletion_proposal(answer)
            if paths:
                _pending_deletions[chat_id] = paths
                display += f"\n\nReply 'yes' to delete these {len(paths)} file(s), or 'no' to cancel."
                print(f"[BOT] Deletion proposal: {len(paths)} files")

            print(f"[BOT] A: {len(display)} chars")
            _send(chat_id, display)


def main() -> None:
    run_bot()


if __name__ == "__main__":
    main()
