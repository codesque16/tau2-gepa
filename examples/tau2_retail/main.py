#!/usr/bin/env python3
"""Tau2 retail agent optimization with GEPA.

Train-only mode: no valset, objective is to maximize score on the training set.
Task IDs to improve on: 12, 17, 23, 27, 32, 33, 34, 45, 42, 43, 56, 57, 66, 68, 78, 73, 86, 81, 91, 113, 102, 103.

Requires tau2: pip install -e /path/to/tau2-mermaid/tau2-bench
Set TAU2_DATA_DIR to tau2-bench/data (or ensure data/ is relative to tau2-bench).

Usage:
  uv run python -m examples.tau2_retail.main
  uv run python -m examples.tau2_retail.main --fresh   # Fresh run: outputs/tau2_retail_MM-DD_HH-MM
"""

import argparse
import os
from datetime import datetime

from examples.tau2_retail.utils import (
    BACKGROUND,
    # OBJECTIVE,           # generalization mode
    evaluate,
    # evaluate_on_dataset, # final comparison on valset
    load_policy_solo_seed,
    # load_tau2_retail_dataset,
    load_tau2_retail_train_only,
    OBJECTIVE_TRAIN_ONLY,
)
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    TrackingConfig,
    optimize_anything,
)

# Config
REFLECTION_LM = "gemini/gemini-3-flash-preview"
LLM_AGENT = "gpt-5-nano"
DIAGNOSIS_LM = "gemini/gemini-3-flash-preview"  # LLM for qualitative ASI on failed tasks
SEED = 7789797979

# Seed candidate: policy_solo.md content (baseline policy)
SEED_CANDIDATE = None  # Loaded from load_policy_solo_seed()


def main():
    parser = argparse.ArgumentParser(description="Tau2 retail agent optimization with GEPA (train-only)")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Create a fresh run directory with timestamp (outputs/tau2_retail_MM-DD_HH-MM)",
    )
    args = parser.parse_args()

    base_dir = "outputs/tau2_retail"
    if args.fresh:
        timestamp = datetime.now().strftime("%m-%d_%H-%M")
        log_dir = f"{base_dir}_{timestamp}"
        print(f"Fresh run: {log_dir}")
    else:
        log_dir = base_dir

    os.makedirs(log_dir, exist_ok=True)

    # Train-only: fixed task IDs, no valset
    train_set = load_tau2_retail_train_only()
    # train_set, val_set = load_tau2_retail_dataset()
    print(f"Dataset: train={len(train_set)} (no valset)")

    seed_candidate = SEED_CANDIDATE or load_policy_solo_seed()

    def evaluator(candidate: str, example) -> tuple[float, SideInfo]:
        return evaluate(
            candidate,
            example,
            llm_agent=LLM_AGENT,
            seed=SEED,
            diagnosis_lm=DIAGNOSIS_LM,
        )

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=log_dir,
            max_metric_calls=100,
            parallel=True,
            max_workers=10,
            cache_evaluation=True,
            track_best_outputs=True,
            candidate_selection_strategy="pareto",
            display_progress_bar=True,
        ),
        reflection=ReflectionConfig(
            reflection_lm=REFLECTION_LM,
            reflection_minibatch_size=10,
        ),
        tracking=TrackingConfig(
            use_wandb=True,
            use_logfire=True,
        ),
    )

    result = optimize_anything(
        seed_candidate=seed_candidate,
        evaluator=evaluator,
        dataset=train_set,
        valset=None,  # train-only: maximize score on training set
        config=config,
        objective=OBJECTIVE_TRAIN_ONLY,
        background=BACKGROUND,
    )

    best_policy = result.best_candidate
    # With valset=None, best is chosen by aggregate score on the same train set
    best_train_score = result.val_aggregate_scores[result.best_idx]
    print(f"\nBest train score: {best_train_score:.4f}")

    out_path = f"{log_dir}/best_policy.md"
    with open(out_path, "w") as f:
        f.write(best_policy or "")
    print(f"Saved: {out_path}")

    # Final comparison (on same training set)
    # print("\nEvaluating baseline (policy_solo.md)...")
    # baseline_score = evaluate_on_dataset(
    #     seed_candidate,
    #     val_set,
    #     llm_agent=LLM_AGENT,
    #     seed=SEED,
    # )
    # print("\nEvaluating best optimized policy...")
    # optimized_score = evaluate_on_dataset(
    #     best_policy,
    #     val_set,
    #     llm_agent=LLM_AGENT,
    #     seed=SEED,
    # )
    # print(f"\nBaseline pass@1:  {baseline_score:.2%}")
    # print(f"Optimized pass@1:  {optimized_score:.2%}")
    # print(f"Improvement:       {optimized_score - baseline_score:+.2%}")


if __name__ == "__main__":
    main()
