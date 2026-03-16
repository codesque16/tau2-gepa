"""Evaluate a calculator prompt on one BODMAS problem via LLM."""

import re
from typing import Any

from examples.arithmetic.data import Problem


def parse_number(text: str) -> float | None:
    """Extract first number from model output (int or float)."""
    if not text or not text.strip():
        return None
    text = text.strip()
    # Allow negative and decimal
    match = re.search(r"-?\d+\.?\d*", text)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def evaluate_one(
    candidate: str,
    example: Problem,
    model: str = "gpt-4o-mini",
) -> tuple[float, dict[str, Any]]:
    """Run one problem: candidate = system prompt, example = Problem.

    Returns (score, side_info). score is 1.0 if answer matches, else 0.0.
    """
    try:
        import litellm
    except ImportError:
        return 0.0, {"error": "litellm not installed", "expr": example.expr}

    user_msg = f"Calculate: {example.expr}\nReply with only the number, nothing else."
    try:
        resp = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": candidate},
                {"role": "user", "content": user_msg},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return 0.0, {"error": str(e), "expr": example.expr}

    got = parse_number(content)
    expected = example.answer
    correct = got is not None and abs(got - expected) < 1e-6
    score = 1.0 if correct else 0.0

    side_info: dict[str, Any] = {
        "expr": example.expr,
        "expected": expected,
        "got": got,
        "raw": content[:200],
    }
    return score, side_info
