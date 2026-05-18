"""Scoring logic for the dibo-agent eval harness.

Two-phase scoring per question:
  1. Programmatic check — fast, free, deterministic (regex / numeric / set / bool)
  2. LLM-as-judge       — single Claude API call, 1-5 integer + reason sentence

The judge always uses claude-sonnet-4-5-20250929 regardless of which model ran
the agent, so the yardstick is consistent across model comparison runs.
"""
from __future__ import annotations

import json
import re
from typing import Any

import anthropic

JUDGE_MODEL = "claude-sonnet-4-5-20250929"
JUDGE_INPUT_RATE = 3.00 / 1_000_000   # $ per token
JUDGE_OUTPUT_RATE = 15.00 / 1_000_000


# ── Programmatic checks ───────────────────────────────────────────────────────

def _check_regex(expected: dict, answer: str) -> tuple[bool, str]:
    """All patterns must match (re.search, case-insensitive)."""
    for pattern in expected.get("patterns", []):
        if not re.search(pattern, answer, re.IGNORECASE):
            return False, f"pattern not found: {pattern!r}"
    return True, "all patterns matched"


def _check_numeric_range(expected: dict, answer: str) -> tuple[bool, str]:
    """Each named numeric value must be extractable and within [low, high]."""
    for spec in expected.get("values", []):
        pattern = spec["extract_pattern"]
        m = re.search(pattern, answer, re.IGNORECASE)
        if not m:
            return False, f"could not extract '{spec['name']}' from answer"
        # Use the first non-None capture group
        raw = next((g for g in m.groups() if g is not None), None)
        if raw is None:
            return False, f"regex matched but no capture group for '{spec['name']}'"
        val = float(raw)
        lo, hi = spec["low"], spec["high"]
        if not (lo <= val <= hi):
            return False, f"{spec['name']} = {val} {spec.get('unit', '')}, outside [{lo}, {hi}]"
    return True, "all values in range"


def _check_set_contains(expected: dict, answer: str) -> tuple[bool, str]:
    """Answer must mention all required items and none of the excluded ones."""
    flags = re.IGNORECASE if expected.get("case_insensitive", True) else 0
    for item in expected.get("required", []):
        if not re.search(re.escape(item), answer, flags):
            return False, f"required item missing: {item!r}"
    for item in expected.get("excluded", []):
        if re.search(re.escape(item), answer, flags):
            return False, f"excluded item present: {item!r}"
    return True, "all required items found, no excluded items present"


def _check_bool_flag(expected: dict, answer: str) -> tuple[bool, str]:
    """Answer must contain a word from the yes-list XOR the no-list."""
    lower = answer.lower()
    yes_hit = any(w.lower() in lower for w in expected.get("yes_words", []))
    no_hit = any(w.lower() in lower for w in expected.get("no_words", []))
    want = expected.get("expected_value", True)
    if want:
        if yes_hit and not no_hit:
            return True, "positive indicator found"
        if no_hit:
            return False, "negative indicator found (expected positive)"
        return False, "no yes/no indicator found"
    else:
        if no_hit and not yes_hit:
            return True, "negative indicator found"
        if yes_hit:
            return False, "positive indicator found (expected negative)"
        return False, "no yes/no indicator found"


def _programmatic_check(expected: dict, answer: str) -> tuple[bool | None, str]:
    """Dispatch to the right checker. Returns (passed, detail)."""
    check_type = expected.get("type", "judge_only")
    if check_type == "judge_only":
        return None, "judge_only — no programmatic check"
    if check_type == "regex":
        return _check_regex(expected, answer)
    if check_type == "numeric_range":
        return _check_numeric_range(expected, answer)
    if check_type == "set_contains":
        return _check_set_contains(expected, answer)
    if check_type == "bool_flag":
        return _check_bool_flag(expected, answer)
    return None, f"unknown check type: {check_type!r}"


# ── LLM-as-judge ─────────────────────────────────────────────────────────────

JUDGE_SYSTEM = (
    "You are evaluating answers from an AI agent that monitors a Linux home server "
    "called dibo. The agent has access to SSH tools, Docker monitoring, AdGuard DNS, "
    "and a historical metrics database. Judge answers on factual correctness, "
    "specificity (numbers/units where relevant), and appropriate use of tools."
)

JUDGE_USER_TEMPLATE = """\
QUESTION: {question}

AGENT ANSWER:
{answer}

RUBRIC:
{rubric}

Score the answer on a 1-5 integer scale:
5 - Correct, specific, complete. Numbers and units present where relevant.
4 - Correct but missing minor details.
3 - Partially correct — some right facts, some missing.
2 - Mostly wrong or vague, but the agent tried the right approach.
1 - Wrong, hallucinated, no tool calls made, or refused without reason.

Respond ONLY with a valid JSON object on a single line:
{{"score": <integer 1-5>, "reason": "<one sentence>"}}"""


def _run_judge(question: str, answer: str, rubric: str) -> dict:
    """Call Claude as judge. Returns {score, reason, cost_usd, model}."""
    if not answer.strip():
        return {
            "score": 1,
            "reason": "Agent returned an empty answer.",
            "cost_usd": 0.0,
            "model": JUDGE_MODEL,
        }

    client = anthropic.Anthropic()
    user_content = JUDGE_USER_TEMPLATE.format(
        question=question, answer=answer[:3000], rubric=rubric
    )
    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=100,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        data = json.loads(raw)
        score = int(data["score"])
        reason = str(data.get("reason", ""))
    except Exception as e:
        return {
            "score": -1,
            "reason": f"Judge failed: {type(e).__name__}: {e}",
            "cost_usd": 0.0,
            "model": JUDGE_MODEL,
        }

    usage = resp.usage
    cost = (
        usage.input_tokens * JUDGE_INPUT_RATE
        + usage.output_tokens * JUDGE_OUTPUT_RATE
    )
    return {
        "score": score,
        "reason": reason,
        "cost_usd": round(cost, 6),
        "model": JUDGE_MODEL,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def score_result(question_spec: dict, answer: str) -> dict:
    """Score a single answer. Returns a dict suitable for inserting into eval_results."""
    expected = question_spec.get("expected", {"type": "judge_only"})
    judge_cfg = question_spec.get("judge", {})
    check_type = expected.get("type", "judge_only")

    programmatic_pass, programmatic_detail = _programmatic_check(expected, answer)

    judge_score, judge_reason, judge_cost, judge_model = -1, "", 0.0, JUDGE_MODEL
    if judge_cfg.get("enabled", False):
        result = _run_judge(
            question=question_spec["question"],
            answer=answer,
            rubric=judge_cfg.get("rubric", ""),
        )
        judge_score = result["score"]
        judge_reason = result["reason"]
        judge_cost = result["cost_usd"]
        judge_model = result["model"]

    # Composite: 0.0 to 1.0
    if check_type == "judge_only":
        composite = judge_score / 5.0 if judge_score > 0 else 0.0
    else:
        prog_component = 1.0 if programmatic_pass else 0.0
        judge_component = judge_score / 5.0 if judge_score > 0 else 0.0
        composite = (prog_component * 2 + judge_component) / 3.0

    return {
        "check_type": check_type,
        "programmatic_pass": programmatic_pass,
        "programmatic_detail": programmatic_detail,
        "judge_score": judge_score,
        "judge_reason": judge_reason,
        "judge_cost_usd": judge_cost,
        "judge_model": judge_model,
        "composite_score": round(composite, 4),
    }
