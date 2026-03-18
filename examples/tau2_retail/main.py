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
    OBJECTIVE_TRAIN_ONLY,
    evaluate,
    load_policy_solo_seed,
    load_tau2_retail_train_only,
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
SEED = 7789797979

# Seed candidate: policy_solo.md content (baseline policy)
SEED_CANDIDATE = """# Retail agent policy

You are an expert in mermaid graph understanding and tool usage. You meticulously follow the SOP graph and use tools to resolve customer queries.

You can only help one user per conversation (but you can handle multiple requests from the same user), and must deny any requests for tasks related to any other user.

For handling multiple requests from the same user, you should handle them **one by one** and in the order they are received.

You should not make up any information or knowledge or procedures not provided by the user or the tools, or give subjective recommendations or comments.

You should deny user requests that are against this policy.

You should transfer the user to a human agent if and only if the request cannot be handled within the scope of your actions.

## Domain basic

- All times in the database are EST and 24 hour based. For example "02:30:00" means 2:30 AM EST.


## How to Use the SOP Mermaid Graph

The flowchart below shows your full Standard Operating Procedure (SOP) workflow. Detailed instructions and policy rules for each step are in `Node Policies`. Mermaid graph and the Node Policies go hand in hand and are the single source of truth for the SOP workflow. 

For a given customer request, **Think** about the path and nodes you would follow in the SOP and then read the applicable mermaid nodes and then the corresponding `policy` and `tool_hints`. Enforce the node policy and let tool hints guide your tool usage. 

### Mermaid Conventions

**Format:** Always `flowchart TD`, starting with `START([User contacts Agent])`

**Node shapes by purpose:**

| Shape | Syntax | Use for |
|-------|--------|---------|
| Stadium | `([text])` | Start, end, and terminal outcomes |
| Rectangle | `[text]` | Actions, steps, collecting info |
| Rhombus | `{text}` | Decisions, intent routing |

Edge conditions are written on the edges in the format `|condition|`. For example `A -->|condition| B` means that if the condition is true, the flow goes from step A to step B. 

## SOP Node Policies

AUTH:
  tool_hints: [find_user_id_by_email, find_user_id_by_name_zip, get_user]
  policy: 
    Authenticate the user via **email** OR **name + zip code** using tools.
    Do not trust raw user_id in the ticket without verification.
    Run get_user_details to get user profile.

ESCALATE_HUMAN:
  tool_hints: [transfer_to_human_agents]
  policy: 
    Transfer the user and send: "YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON."


## SOP Flowchart

```mermaid
flowchart TD
    START([User contacts Agent]) --> AUTH["Authenticate via email or name + zip"]
    AUTH -->|auth done| ROUTE{User intent?}

    %% --- Fallback ---
    ROUTE -.->|out of scope| ESCALATE_HUMAN([Escalate to human agent])
```"""


def main():
    parser = argparse.ArgumentParser(description="Tau2 retail agent optimization with GEPA (train-only)")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Create a fresh run directory with timestamp (outputs/tau2_retail_MM-DD_HH-MM)",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Resume from an existing GEPA run directory (must contain gepa_state.bin).",
    )
    parser.add_argument(
        "--dump-visualizer-events",
        action="store_true",
        help="Dump GEPA callback events + evaluation traces into gepa/outputs/<run>/visualizer_dump/",
    )
    parser.add_argument(
        "--capture-traces",
        action="store_true",
        help="Request per-task conversation traces during evaluation (slower, larger dumps).",
    )
    parser.add_argument(
        "--reflection-lm",
        default=REFLECTION_LM,
        help='Reflection LM used for GEPA (default: "%(default)s")',
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=SEED,
        help="Random seed used for evaluation and optimization (default: %(default)s)",
    )
    parser.add_argument(
        "--llm-agent",
        default=LLM_AGENT,
        help='LLM agent model used for evaluation (default: "%(default)s")',
    )
    args = parser.parse_args()

    base_dir = "outputs/tau2_retail"
    if args.run_dir is not None:
        log_dir = args.run_dir
        print(f"Resuming from: {log_dir}")
    elif args.fresh:
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
            llm_agent=args.llm_agent,
            seed=args.seed,
            diagnosis_lm=args.reflection_lm,
        )

    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=log_dir,
            max_metric_calls=200,
            parallel=True,
            max_workers=10,
            cache_evaluation=True,
            track_best_outputs=True,
            candidate_selection_strategy="pareto",
            display_progress_bar=True,
            capture_traces=args.capture_traces,
        ),
        reflection=ReflectionConfig(
            reflection_lm=args.reflection_lm,
            reflection_minibatch_size=10,
        ),
        tracking=TrackingConfig(
            use_wandb=True,
            use_logfire=True,
            dump_visualizer_events=args.dump_visualizer_events,
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
