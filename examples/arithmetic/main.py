#!/usr/bin/env python3
"""BODMAS prompt optimization with GEPA.

Optimizes a system prompt so an LLM correctly evaluates arithmetic expressions
(BODMAS order). Fast, small example to test the framework and logging.

Usage:
  uv run python -m examples.arithmetic.main
  uv run python -m examples.arithmetic.main --fresh
  uv run python -m examples.arithmetic.main --no-wandb --no-logfire   # minimal deps
"""

import argparse
import os
from datetime import datetime
import hashlib

from examples.arithmetic.data import get_train_val
from examples.arithmetic.evaluate import evaluate_one
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    TrackingConfig,
    optimize_anything,
)

# Budget for 50 train + 50 val: enough for seed eval + proposal iterations
MAX_METRIC_CALLS = 400
REFLECTION_LM = "gemini/gemini-3-flash-preview"
EVAL_MODEL = "gpt-4o-mini"

OBJECTIVE = "Maximize the fraction of BODMAS arithmetic problems solved correctly. The model must show steps and end with 'Final answer: N'."
BACKGROUND = """You are improving a system prompt for an LLM that evaluates arithmetic (BODMAS).
- The user sends one expression. The model must show BODMAS steps (Brackets, then ×/÷, then +/−) and end with exactly: Final answer: <number>.
- Output format: use clear step lines (e.g. Step 1: ..., Step 2: ...) and a final line "Final answer: N" so the answer can be parsed.
- Many models ignore order of operations or omit the answer token. Your prompt should require: (1) show steps in order, (2) end with "Final answer: N"."""

SEED_PROMPT = """You are a precise calculator. Evaluate the expression using BODMAS (Brackets, then ×/÷, then +/−). Show each step in order (e.g. Step 1: ..., Step 2: ...). End with exactly one line: Final answer: <number>. No other text after the final answer."""
SEED_PROMPT = """You are a calculator. Evaluate the expression. Show each step in order (e.g. Step 1: ..., Step 2: ...). End with exactly one line: Final answer: <number>"""

def main() -> None:
    parser = argparse.ArgumentParser(description="BODMAS prompt optimization with GEPA")
    parser.add_argument("--fresh", action="store_true", help="Fresh run dir with timestamp")
    parser.add_argument("--no-wandb", action="store_true", help="Disable wandb")
    parser.add_argument("--no-logfire", action="store_true", help="Disable logfire")
    parser.add_argument("--max-calls", type=int, default=MAX_METRIC_CALLS, help="Max evaluation budget")
    args = parser.parse_args()

    train_set, val_set = get_train_val()
    print(f"Dataset: train={len(train_set)}, val={len(val_set)}")

    base_dir = "outputs/arithmetic"
    if args.fresh:
        log_dir = f"{base_dir}_{datetime.now():%m-%d_%H-%M}"
    else:
        log_dir = base_dir
    os.makedirs(log_dir, exist_ok=True)
    print(f"Run dir: {log_dir}")

    def evaluator(candidate: str, example) -> tuple[float, SideInfo]:
        """Evaluate one arithmetic problem with logging-friendly candidate hash."""
        cand_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
        try:
            import logfire
        except ImportError:
            score, side = evaluate_one(candidate, example, model=EVAL_MODEL)
            side.setdefault("candidate_sha256_12", cand_hash)
            return score, side

        # Include hash in span name for quick visual identification in Logfire.
        span_name = f"gepa_eval_arith cand={cand_hash}"
        with logfire.span(span_name, candidate_sha256_12=cand_hash):
            score, side = evaluate_one(candidate, example, model=EVAL_MODEL)
            side.setdefault("candidate_sha256_12", cand_hash)
            return score, side

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=log_dir,
            max_metric_calls=args.max_calls,
            parallel=True,
            max_workers=50,  # evaluate train/val sets concurrently (50 train + 50 val)
            cache_evaluation=True,
            track_best_outputs=False,
            candidate_selection_strategy="pareto",
            display_progress_bar=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=REFLECTION_LM,
            reflection_minibatch_size=10,
        ),
        tracking=TrackingConfig(
            use_wandb=not args.no_wandb,
            use_logfire=not args.no_logfire,
        ),
    )

    result = optimize_anything(
        seed_candidate=SEED_PROMPT,
        evaluator=evaluator,
        dataset=train_set,
        valset=val_set,
        config=config,
        objective=OBJECTIVE,
        background=BACKGROUND,
    )

    best_prompt = result.best_candidate
    best_val = result.val_aggregate_scores[result.best_idx]
    print(f"\nBest val accuracy: {best_val:.2%}")
    out_path = f"{log_dir}/best_prompt.txt"
    with open(out_path, "w") as f:
        f.write(best_prompt or "")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
