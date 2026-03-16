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

# Budget for 12 train + 4 val: enough for seed eval + a few proposal iterations
MAX_METRIC_CALLS = 50
REFLECTION_LM = "gpt-4o-mini"
EVAL_MODEL = "gpt-4o-mini"

OBJECTIVE = "Maximize the fraction of BODMAS arithmetic problems solved correctly (one number per problem)."
BACKGROUND = """You are improving a system prompt for an LLM that acts as a calculator.
- The user sends one arithmetic expression (e.g. "3 + 4 * 2").
- The model must reply with only the final number, using BODMAS/BIDMAS order (brackets, then multiplication/division, then addition/subtraction).
- Many models ignore order of operations unless the prompt is explicit. Your prompt should stress: evaluate in correct order, output only the number."""

SEED_PROMPT = """You are a precise calculator. Evaluate the expression using standard order of operations (BODMAS). Reply with only the final number, no explanation."""


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
        return evaluate_one(candidate, example, model=EVAL_MODEL)

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=log_dir,
            max_metric_calls=args.max_calls,
            parallel=False,  # sequential for simplicity and deterministic logs
            max_workers=1,
            cache_evaluation=True,
            track_best_outputs=False,
            candidate_selection_strategy="pareto",
            display_progress_bar=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=REFLECTION_LM,
            reflection_minibatch_size=2,
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
