"""Optional **tool_code** surface: validate proposed Python with compile (+ optional [Monty](https://github.com/pydantic/monty)), curator fix loop, then drop code on failure.

This module is intentionally independent of the solo simulator. GEPA callbacks cannot mutate
candidates; the gate runs inside :func:`evaluate_policy_with_mermaid_components` before the retail run.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

CuratorFn = Callable[[str, str], str]
"""``(code, error_or_hint) -> revised_code``."""


def _compile_only(code: str) -> tuple[bool, str | None]:
    try:
        compile(code, "<tool_code>", "exec")
    except SyntaxError as e:
        return False, f"syntax: {e}"
    except Exception as e:
        return False, repr(e)
    return True, None


def _monty_smoke(code: str) -> tuple[bool, str | None]:
    """Best-effort execution smoke test via pydantic-monty (optional dependency)."""
    try:
        import pydantic_monty  # type: ignore
    except ImportError:
        return True, None
    try:
        m = pydantic_monty.Monty(
            code,
            inputs=[],
            script_name="tool_code.py",
        )
        m.run(external_functions={})
    except Exception as e:
        return False, f"monty: {e!r}"
    return True, None


def apply_tool_code_gate(
    candidate: dict[str, str],
    *,
    enabled: bool,
    max_rounds: int,
    use_monty: bool,
    curator: CuratorFn | None,
    strict_fail_score: bool,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return ``(candidate_out, meta)``. On failure, ``tool_code`` is cleared if it was non-empty.

    If ``strict_fail_score`` and the caller uses failed gate for scoring, treat as hard failure
    (handled in the evaluator, not here).
    """
    meta: dict[str, Any] = {
        "skipped": False,
        "accepted": None,
        "rounds": [],
        "strict_fail_score": strict_fail_score,
        "initial_nonempty": False,
    }
    if not enabled:
        meta["skipped"] = True
        return candidate, meta

    raw = (candidate.get("tool_code") or "").strip()
    meta["initial_nonempty"] = bool(raw)
    if not raw:
        meta["skipped"] = True
        meta["accepted"] = True
        return candidate, meta

    code = raw
    for r in range(max(1, max_rounds)):
        ok, err = _compile_only(code)
        meta["rounds"].append({"round": r, "stage": "compile", "ok": ok, "detail": err})
        if ok and use_monty:
            ok_m, err_m = _monty_smoke(code)
            meta["rounds"].append({"round": r, "stage": "monty", "ok": ok_m, "detail": err_m})
            ok = ok_m
            err = err_m
        if ok:
            out = copy.deepcopy(candidate)
            out["tool_code"] = code
            meta["accepted"] = True
            return out, meta
        if curator is None:
            break
        nxt = curator(code, err or "unknown").strip()
        if not nxt or nxt == code:
            break
        code = nxt

    out = copy.deepcopy(candidate)
    out["tool_code"] = ""
    meta["accepted"] = False
    meta["dropped_reason"] = "tool_code_gate_failed"
    return out, meta


def merged_tools_markdown(
    candidate: dict[str, str],
    *,
    monty_label: bool = True,
) -> str:
    """Core ``tools_markdown`` plus optional fenced ``tool_code`` for the agent-facing temp file."""
    base = (candidate.get("tools_markdown") or "").rstrip()
    tc = (candidate.get("tool_code") or "").strip()
    if not tc:
        return base
    gate = "compile" + ("/Monty" if monty_label else "")
    return (
        base
        + "\n\n---\n\n## Additional tools (GEPA ``tool_code`` surface)\n\n"
        + f"Python below is gated ({gate}) before simulation; built-in MCP tools are unchanged — "
        + "this block is design notes / pseudo-tool code for the policy.\n\n"
        + "```python\n"
        + tc
        + "\n```\n"
    )
