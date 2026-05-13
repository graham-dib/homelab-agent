"""Track per-question token usage and cost."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from homelab_agent.ingest.schema import get_connection


# Sonnet 4.5 pricing as of May 2026 ($ per million tokens).
# Update if Anthropic changes pricing or you switch models.
PRICING = {
    "claude-sonnet-4-5-20250929": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
}


def estimate_cost(model: str, usage: dict) -> float:
    """Compute USD cost from a usage dict. Returns 0.0 if model unknown."""
    rates = PRICING.get(model)
    if not rates:
        return 0.0
    return (
        usage.get("input_tokens", 0) * rates["input"] / 1_000_000
        + usage.get("output_tokens", 0) * rates["output"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0) * rates["cache_read"] / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * rates["cache_write"] / 1_000_000
    )


class UsageTracker(BaseCallbackHandler):
    """Accumulates token usage across an agent run, persists on completion."""

    def __init__(self, question: str):
        self.question = question[:500]
        self.model: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_write = 0
        self.n_calls = 0
        self.start_time = time.time()

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """Called after every LLM response within the agent loop."""
        self.n_calls += 1
        try:
            msg = response.generations[0][0].message
            meta = getattr(msg, "response_metadata", {}) or {}
            usage = meta.get("usage", {}) or {}
            if not self.model:
                self.model = meta.get("model_name") or meta.get("model")
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
            self.cache_read += usage.get("cache_read_input_tokens", 0)
            self.cache_write += usage.get("cache_creation_input_tokens", 0)
        except (AttributeError, IndexError, KeyError):
            pass

    def persist(self) -> dict:
        """Write the accumulated usage to the metrics DB. Returns a summary."""
        usage = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read,
            "cache_creation_input_tokens": self.cache_write,
        }
        cost = estimate_cost(self.model or "", usage)
        latency = time.time() - self.start_time

        record = {
            "id": int(time.time() * 1_000_000),
            "timestamp": datetime.now(timezone.utc),
            "question": self.question,
            "model": self.model,
            "n_llm_calls": self.n_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
            "estimated_cost_usd": cost,
            "latency_seconds": latency,
        }

        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO agent_usage
                   (id, timestamp, question, model, n_llm_calls,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    estimated_cost_usd, latency_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                list(record.values()),
            )
            conn.commit()
        finally:
            conn.close()

        return record