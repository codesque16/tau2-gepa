#!/usr/bin/env python3
"""GEPA optimization loop using Claude Code subagents for evaluation and reflection.

Architecture:
  - Evaluation:  claude --agents '{policy injected as system prompt}' -p '<ticket>'
                 Uses the tau2-retail-eval dynamic agent (policy = current GEPA candidate).
                 Trace is parsed; DB hash + communicate_info determine the score.

  - Reflection:  claude --agent tau2-retail-reflector -p '<current_policy + failures>'
                 Reads failure records, proposes an improved policy text.

  - GEPA engine: optimize_anything() maintains the Pareto frontier, state, and iteration.
                 The engine calls cc_evaluate() and cc_propose() at each step.

Usage:
    uv run python -m examples.tau2_retail.claude_code_main
    uv run python -m examples.tau2_retail.claude_code_main --eval-model haiku --reflect-model sonnet
    uv run python -m examples.tau2_retail.claude_code_main --task-ids 0 5 12 --max-calls 50
    uv run python -m examples.tau2_retail.claude_code_main --fresh --output-dir outputs/cc_run_01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
GEPA_DIR = REPO_ROOT / "gepa"
TASKS_PATH = REPO_ROOT / "domains/retail/tasks_solo_comms.json"
DB_PATH = REPO_ROOT / "domains/retail/db.json"
MCP_COMMAND = "uv run domains/retail/tools.py"
SEED_POLICY_PATH = REPO_ROOT / "tau2-bench/data/tau2/domains/retail/policy_base_gepa.md"

sys.path.insert(0, str(REPO_ROOT / "domains/retail"))
from evaluate import evaluate_task_db  # noqa: E402

# ---------------------------------------------------------------------------
# Retail tool names (for --agents JSON mcpServers tool allowlist)
# ---------------------------------------------------------------------------

RETAIL_TOOL_NAMES = [
    "calculate",
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "find_user_id_by_email",
    "find_user_id_by_name_zip",
    "get_order_details",
    "get_product_details",
    "get_user_details",
    "list_all_product_types",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "transfer_to_human_agents",
]

# ---------------------------------------------------------------------------
# Task loading
# ---------------------------------------------------------------------------

_TASKS_BY_ID: dict[str, dict] = {}


def _load_tasks_index() -> dict[str, dict]:
    global _TASKS_BY_ID
    if not _TASKS_BY_ID:
        for t in json.loads(TASKS_PATH.read_text()):
            _TASKS_BY_ID[str(t["id"])] = t
    return _TASKS_BY_ID


# ---------------------------------------------------------------------------
# Trace parsing  (Claude Code JSONL → flat history)
# ---------------------------------------------------------------------------

_MCP_PREFIX = "mcp__retail-tools__"


def _strip_mcp_prefix(name: str) -> str:
    return name[len(_MCP_PREFIX):] if name.startswith(_MCP_PREFIX) else name


def _parse_claude_trace(jsonl_path: Path) -> list[dict[str, Any]]:
    """Convert Claude Code JSONL to flat history for evaluate_task_db."""
    history: list[dict[str, Any]] = []
    for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        msg = event.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        tool_calls, text_parts, content_blocks = [], [], []
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "tool_use":
                name = _strip_mcp_prefix(block.get("name") or "")
                args = block.get("input") or {}
                if name:
                    tool_calls.append({"name": name, "arguments": args})
                    content_blocks.append({"type": "tool_use", "name": name, "arguments": args})
            elif btype == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    text_parts.append(txt)
                    content_blocks.append({"type": "text", "text": txt})
        if tool_calls or text_parts:
            history.append({
                "role": "assistant",
                "tool_calls": tool_calls,
                "content": "\n".join(text_parts) or None,
                "content_blocks": content_blocks,
            })
    return history


def _extract_final_reply(history: list[dict]) -> str:
    for turn in reversed(history):
        if turn.get("content"):
            return turn["content"]
    return ""


def _format_tool_calls(history: list[dict]) -> str:
    lines = []
    for turn in history:
        for tc in turn.get("tool_calls") or []:
            args_str = json.dumps(tc.get("arguments") or {}, ensure_ascii=False)
            lines.append(f"  {tc['name']}({args_str})")
    return "\n".join(lines) or "  (no tool calls)"


# ---------------------------------------------------------------------------
# Trace file locator
# ---------------------------------------------------------------------------

def _project_slug() -> str:
    return str(REPO_ROOT).replace("/", "-")


def _find_trace(session_id: str) -> Path | None:
    base = Path.home() / ".claude" / "projects" / _project_slug()
    candidate = base / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Evaluation via Claude Code subagent
# ---------------------------------------------------------------------------

def cc_evaluate(
    candidate: str | dict[str, str],
    example,  # TaskExample or dict with task_id
    eval_model: str | None = None,
    run_dir: Path | None = None,
) -> tuple[float, dict[str, Any]]:
    """Run the candidate policy on one task via Claude Code and return (score, side_info).

    The candidate policy is injected as the system prompt of a dynamic eval agent
    via --agents JSON, keeping parallel runs isolated (no shared agent file).
    """
    policy = candidate if isinstance(candidate, str) else next(iter(candidate.values()))
    task_id = getattr(example, "task_id", None) or str(example)
    task = _load_tasks_index().get(str(task_id)) or {}
    ticket = task.get("ticket", f"task_id={task_id}")

    session_id = str(uuid.uuid4())

    # Build a session-scoped agent definition with the current candidate as system prompt
    agent_def = {
        "tau2-retail-eval": {
            "description": "Retail eval agent for GEPA optimization.",
            "prompt": policy,
            "tools": RETAIL_TOOL_NAMES,
            "mcpServers": ["retail-tools"],
            "model": eval_model or "inherit",
        }
    }

    cmd = [
        "claude",
        "--agents", json.dumps(agent_def),
        "--agent", "tau2-retail-eval",
        "--session-id", session_id,
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "-p", ticket,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    # Extract text response from JSON output
    final_reply = ""
    try:
        out = json.loads(proc.stdout)
        final_reply = out.get("result") or out.get("response") or proc.stdout
    except json.JSONDecodeError:
        final_reply = proc.stdout

    # Parse trace and evaluate
    score = 0.0
    db_match = False
    communicate_match: bool | None = None
    tool_call_summary = "(trace not found)"
    history: list[dict] = []

    trace_path = _find_trace(session_id)
    if trace_path and trace_path.exists():
        history = _parse_claude_trace(trace_path)
        tool_call_summary = _format_tool_calls(history)

        try:
            db_result = asyncio.run(evaluate_task_db(
                task=task,
                assistant_history=history,
                db_path=DB_PATH,
                mcp_command=MCP_COMMAND,
            ))
            db_match = bool(db_result.get("db_match"))
            score = 1.0 if db_match else 0.0

            # communicate_info check (substring, no LLM)
            communicate_info = (task.get("evaluation_criteria") or {}).get("communicate_info") or []
            if communicate_info:
                reply_lower = final_reply.lower().replace(",", "")
                communicate_match = all(
                    s.lower().replace(",", "") in reply_lower
                    for s in communicate_info
                )
        except Exception as exc:
            db_result = {"error": str(exc)}

    # Copy trace for inspection (if run_dir provided)
    if run_dir and trace_path and trace_path.exists():
        run_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(trace_path, run_dir / f"task_{task_id}.jsonl")
        (run_dir / f"task_{task_id}.txt").write_text(final_reply, encoding="utf-8")

    side_info: dict[str, Any] = {
        "task_id": task_id,
        "ticket": ticket,
        "score": score,
        "db_match": db_match,
        "communicate_match": communicate_match,
        "tool_calls": tool_call_summary,
        "final_reply": final_reply[:600] + ("…" if len(final_reply) > 600 else ""),
        "session_id": session_id,
    }
    if proc.returncode != 0 and proc.stderr:
        side_info["stderr"] = proc.stderr[:300]

    return score, side_info


# ---------------------------------------------------------------------------
# Reflection via Claude Code subagent
# ---------------------------------------------------------------------------

def cc_propose(
    candidate: dict[str, str],
    reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
    components_to_update: list[str],
    reflect_model: str | None = None,
) -> dict[str, str]:
    """Propose improved policy text by running the tau2-retail-reflector subagent.

    This is used as GEPA's custom_candidate_proposer. It receives the current
    candidate and the failure records from make_reflective_dataset(), formats
    them into a prompt, and calls the reflector subagent.
    """
    component = components_to_update[0] if components_to_update else next(iter(candidate))
    current_policy = candidate.get(component, next(iter(candidate.values())))
    records = list(reflective_dataset.get(component) or reflective_dataset.get(next(iter(reflective_dataset))) or [])

    # Format failure records for the reflector
    failures = []
    for r in records:
        task_id = r.get("task_id", "?")
        score = r.get("score", r.get("Scores (Higher is Better)", "?"))
        ticket = r.get("ticket", "")
        tool_calls = r.get("tool_calls", "")
        final_reply = r.get("final_reply", "")
        communicate_match = r.get("communicate_match")
        failure_block = (
            f"Task {task_id} — score={score}"
            + (f", communicate_match={communicate_match}" if communicate_match is not None else "")
            + f"\nTicket: {ticket[:300]}"
            + f"\nTool calls:\n{tool_calls}"
            + f"\nFinal reply: {final_reply[:400]}"
        )
        failures.append(failure_block)

    n_pass = sum(1 for r in records if float(r.get("score", r.get("Scores (Higher is Better)", 0))) >= 1.0)
    prompt = (
        f"<current_policy>\n{current_policy}\n</current_policy>\n\n"
        f"<evaluation_results>\n"
        f"Tasks evaluated: {len(records)}  |  Passed: {n_pass}  |  Failed: {len(records) - n_pass}\n\n"
        + "\n\n---\n\n".join(failures)
        + "\n</evaluation_results>\n\n"
        "Propose an improved policy based on the failures above."
    )

    cmd = [
        "claude",
        "--agent", "tau2-retail-reflector",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "-p", prompt,
    ]
    if reflect_model:
        cmd += ["--model", reflect_model]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    new_policy = ""
    try:
        out = json.loads(proc.stdout)
        new_policy = out.get("result") or out.get("response") or proc.stdout
    except json.JSONDecodeError:
        new_policy = proc.stdout

    new_policy = new_policy.strip()
    if not new_policy or not new_policy.startswith("#"):
        # Fallback: return current policy unchanged
        print(f"[reflector] WARNING: unexpected output, keeping current policy. stderr={proc.stderr[:200]}")
        new_policy = current_policy

    return {component: new_policy}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="GEPA optimization with Claude Code subagents")
    parser.add_argument("--eval-model", default=None, help="Model for eval agent (haiku/sonnet/opus)")
    parser.add_argument("--reflect-model", default=None, help="Model for reflection agent (sonnet/opus)")
    parser.add_argument("--task-ids", nargs="+", help="Task IDs to optimize on (default: TRAIN_ONLY_TASK_IDS)")
    parser.add_argument("--max-calls", type=int, default=200, help="Max metric calls (default: 200)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel eval workers (default: 4)")
    parser.add_argument("--minibatch", type=int, default=6, help="Reflection minibatch size (default: 6)")
    parser.add_argument("--seed-policy", default=None, help="Path to seed policy file (default: policy_base_gepa.md)")
    parser.add_argument("--fresh", action="store_true", help="Force new run dir")
    parser.add_argument("--output-dir", default=None, help="Output directory for traces and best policy")
    args = parser.parse_args()

    # Lazy import of gepa from within the gepa package
    sys.path.insert(0, str(GEPA_DIR / "src"))
    from examples.tau2_retail.utils import TaskExample, TRAIN_ONLY_TASK_IDS  # noqa: PLC0415
    from gepa.optimize_anything import (  # noqa: PLC0415
        GEPAConfig, EngineConfig, ReflectionConfig, TrackingConfig, optimize_anything,
    )

    # Load seed
    seed_path = Path(args.seed_policy) if args.seed_policy else SEED_POLICY_PATH
    seed_policy = seed_path.read_text(encoding="utf-8")
    print(f"Seed policy: {seed_path.name} ({len(seed_policy)} chars)")

    # Load tasks
    task_ids = args.task_ids or TRAIN_ONLY_TASK_IDS
    train_set = [TaskExample(task_id=tid) for tid in task_ids]
    print(f"Train set: {len(train_set)} tasks — {task_ids}")

    # Output dir for traces
    from datetime import datetime  # noqa: PLC0415
    ts = datetime.now().strftime("%m%d_%H%M")
    run_dir = Path(args.output_dir) if args.output_dir else Path(f"outputs/cc_gepa_{ts}")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run dir: {run_dir.resolve()}\n")

    # Bind model args into closures
    eval_model = args.eval_model
    reflect_model = args.reflect_model

    def evaluator(candidate: str | dict, example: TaskExample) -> tuple[float, dict]:
        return cc_evaluate(candidate, example, eval_model=eval_model, run_dir=run_dir / "traces")

    def proposer(candidate, reflective_dataset, components_to_update) -> dict[str, str]:
        return cc_propose(candidate, reflective_dataset, components_to_update, reflect_model=reflect_model)

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=str(run_dir / "gepa_state"),
            max_metric_calls=args.max_calls,
            parallel=True,
            max_workers=args.workers,
            cache_evaluation=True,
            track_best_outputs=True,
            candidate_selection_strategy="pareto",
            display_progress_bar=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=None,  # not needed — we use custom proposer
            reflection_minibatch_size=args.minibatch,
            custom_candidate_proposer=proposer,
        ),
        tracking=TrackingConfig(
            use_wandb=False,
            use_logfire=False,
        ),
    )

    result = optimize_anything(
        seed_candidate={"policy": seed_policy},
        evaluator=evaluator,
        dataset=train_set,
        valset=None,
        objective="Maximize score on each task. Score is 1.0 (pass) or 0.0 (fail).",
        config=config,
    )

    best_policy = result.best_candidate
    if isinstance(best_policy, dict):
        best_policy = next(iter(best_policy.values()))

    best_score = result.val_aggregate_scores[result.best_idx]
    print(f"\nBest train score: {best_score:.4f}")

    out_path = run_dir / "best_policy.md"
    out_path.write_text(best_policy or "", encoding="utf-8")
    print(f"Saved best policy: {out_path}")


if __name__ == "__main__":
    main()
