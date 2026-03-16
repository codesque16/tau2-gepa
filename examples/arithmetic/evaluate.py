"""Evaluate a calculator prompt on one BODMAS problem via LLM."""

import re
from typing import Any

from examples.arithmetic.data import Problem


def _calculator(expr: str) -> float | None:
    """Evaluator-only: compute expression (BODMAS). Used for feedback, not for the LLM."""
    expr = expr.strip()
    if not expr or not re.match(r"^[\d\s+\-*/().]+$", expr):
        return None
    try:
        return float(eval(expr))
    except Exception:
        return None


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


def parse_final_answer(content: str) -> float | None:
    """Prefer 'Final answer: N' or 'ANSWER: N'; fallback to first number."""
    match = re.search(r"(?:Final answer|ANSWER)\s*:\s*(-?\d+\.?\d*)", content, re.I)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return parse_number(content)


def _has_answer_token(content: str) -> bool:
    """True if content contains 'Final answer: N' or 'ANSWER: N'."""
    return bool(re.search(r"(?:Final answer|ANSWER)\s*:\s*-?\d+\.?\d*", content, re.I))


def _count_step_lines(content: str) -> int:
    """Count lines that look like 'Step N:' or 'Phase ...' (optional structure)."""
    count = 0
    for line in content.splitlines():
        line = line.strip()
        if re.match(r"^Step\s+\d+\s*[:.]", line, re.I) or re.match(r"^Phase\s+", line, re.I):
            count += 1
    return count


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

    user_msg = (
        f"Evaluate the following using BODMAS. Show each step in order "
        f"(Brackets, then ×/÷, then +/−). End with exactly: Final answer: <number>.\n\n"
        f"Expression: {example.expr}"
    )
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

    got = parse_final_answer(content)
    expected = _calculator(example.expr)
    if expected is None:
        expected = example.answer
    format_ok = _has_answer_token(content)
    steps_found = _count_step_lines(content)
    answer_correct = got is not None and abs(got - expected) < 1e-6
    score = 1.0 if answer_correct else 0.0

    if answer_correct:
        feedback = "Correct."
    elif got is None:
        if not format_ok:
            feedback = f"No 'Final answer: N' line. Expected {expected}. Expression: {example.expr}."
        else:
            feedback = f"No valid answer found. Expected {expected} (expression: {example.expr})."
    else:
        feedback = f"Wrong. Expected {expected}, got {got}. Expression: {example.expr}."
    if not format_ok and answer_correct is False and got is not None:
        feedback += " Prefer ending with 'Final answer: N' for parsing."
    if steps_found == 0 and not answer_correct:
        feedback += " No step lines (Step 1: ... or Phase ...) detected."

    side_info: dict[str, Any] = {
        "expr": example.expr,
        "expected": expected,
        "got": got,
        "raw": content[:200],
        "feedback": feedback,
        "format_ok": format_ok,
        "steps_found": steps_found,
        "answer_correct": answer_correct,
    }
    return score, side_info
