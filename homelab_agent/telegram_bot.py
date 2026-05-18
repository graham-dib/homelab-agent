"""Two-way Telegram bot — polls for messages and routes them to the homelab agent.

Only responds to the chat ID set in TELEGRAM_CHAT_ID (your personal chat).
Messages from any other chat are silently ignored.

Deploy:
    sudo cp deploy/dibo-telegram.service /etc/systemd/system/
    sudo systemctl enable --now dibo-telegram.service
"""
from __future__ import annotations

import sys
import time
from uuid import uuid4

import httpx
from dotenv import load_dotenv

load_dotenv()

from homelab_agent.config import settings

POLL_TIMEOUT = 30   # long-poll seconds per getUpdates call


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


def _run_agent(question: str) -> str:
    from homelab_agent.agent import build_agent
    from homelab_agent.cost_tracking import UsageTracker

    agent = build_agent()
    tracker = UsageTracker(question)
    try:
        result = agent.invoke(
            {"messages": [("user", question)]},
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
                return content.strip()
    except Exception as e:
        tracker.persist()
        return f"[Agent error: {type(e).__name__}: {e}]"

    tracker.persist()
    return "(no answer returned)"


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
            _typing(chat_id)

            answer = _run_agent(text)
            print(f"[BOT] A: {len(answer)} chars")
            _send(chat_id, answer)


def main() -> None:
    run_bot()


if __name__ == "__main__":
    main()
