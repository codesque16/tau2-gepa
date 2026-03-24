#!/usr/bin/env python3
"""Tau2 retail agent optimization with GEPA.

Train-only mode: no valset, objective is to maximize score on the training set.
Task IDs to improve on: 12, 17, 23, 27, 32, 33, 34, 45, 42, 43, 56, 57, 66, 68, 78, 73, 86, 81, 91, 113, 102, 103.

Requires tau2: pip install -e /path/to/tau2-mermaid/tau2-bench
Set TAU2_DATA_DIR to tau2-bench/data (or ensure data/ is relative to tau2-bench).

Objective and background default to ``reflection_prompts.md`` (# Objective / # Background).
Optional ``# Optimizer`` is a **full** reflection prompt template: include ``<objective>``, ``<background>``,
``<curr_param>``, and ``<side_info>`` (the last two are filled by GEPA at runtime). If ``# Optimizer`` is
omitted, GEPA builds the reflection prompt from objective/background only (internal default builder).
For **partial** merge (fixed prefix + generated region), pass ``--gepa-template-file`` with plain text that
includes ``<gepa_generated>`` (see GEPA ``_apply_gepa_generated_template``). Omit that flag for **full-candidate**
reflection (entire policy rewritten each iteration).

Usage:
  uv run python -m examples.tau2_retail.main --seed-candidate-file examples/tau2_retail/seed_solo_v1.md
  uv run python -m examples.tau2_retail.main --gepa-template-file path/to/template.txt
  uv run python -m examples.tau2_retail.main --reflection-prompts-file path/to/prompts.md
  uv run python -m examples.tau2_retail.main --fresh --seed-candidate-file path/to/seed.md
  uv run python -m examples.tau2_retail.main   # default seed: tau2 retail policy_solo.md; prompts: reflection_prompts.md
  uv run python -m examples.tau2_retail.main --run-dir outputs/tau2_retail_03-18_13-11-45
"""

import argparse
import os
from datetime import datetime
from pathlib import Path

from examples.tau2_retail.reflection_prompts_md import (
    build_reflection_prompt_from_optimizer_template,
    load_gepa_template_file,
    load_reflection_prompts_file,
)
from examples.tau2_retail.utils import (
    BACKGROUND,
    OBJECTIVE_TRAIN_ONLY,
    evaluate,
    load_policy_solo_seed,
    load_tau2_retail_train_only,
    resolve_policy_solo_seed_path,
)
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    ReflectionConfig,
    SideInfo,
    TrackingConfig,
    optimize_anything,
)

_EXAMPLE_DIR = Path(__file__).resolve().parent
_DEFAULT_REFLECTION_PROMPTS = _EXAMPLE_DIR / "reflection_prompts.md"

# Config
REFLECTION_LM = "gemini/gemini-3-flash-preview"
LLM_AGENT = "gpt-5-nano"
SEED = 7789797979


def _resolve_reflection_prompts(
    path_arg: str | None,
) -> tuple[str, str, str | None, str | None, str | None]:
    """Return (objective, background, prompts_path_for_log_or_none, optimizer_or_none, evaluator_prompt_template_or_none)."""
    if path_arg is None:
        if _DEFAULT_REFLECTION_PROMPTS.is_file():
            p = _DEFAULT_REFLECTION_PROMPTS.resolve()
            bundle = load_reflection_prompts_file(p)
            return (
                bundle.objective,
                bundle.background,
                str(p),
                bundle.optimizer,
                bundle.evaluator_prompt_template,
            )
        return OBJECTIVE_TRAIN_ONLY, BACKGROUND, None, None, None

    p = Path(path_arg).expanduser().resolve()
    bundle = load_reflection_prompts_file(p)
    return (
        bundle.objective,
        bundle.background,
        str(p),
        bundle.optimizer,
        bundle.evaluator_prompt_template,
    )


def main():
    parser = argparse.ArgumentParser(description="Tau2 retail agent optimization with GEPA (train-only)")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Create a fresh run directory with timestamp (outputs/tau2_retail_MM-DD_HH-MM-SS)",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Resume from an existing GEPA run directory (must contain gepa_state.bin).",
    )
    parser.add_argument(
        "--seed-candidate-file",
        default=None,
        metavar="PATH",
        help=(
            "Markdown file to use as the seed policy. "
            "Default: tau2 retail policy_solo.md (under TAU2_DATA_DIR)."
        ),
    )
    parser.add_argument(
        "--reflection-prompts-file",
        default=None,
        metavar="PATH",
        help=(
            "Markdown: # Objective, # Background (fenced). Optional # Optimizer = full reflection template "
            "with <objective>, <background>, <curr_param>, <side_info>. "
            f"Default: {_DEFAULT_REFLECTION_PROMPTS.name} if present, else utils strings."
        ),
    )
    parser.add_argument(
        "--gepa-template-file",
        default=None,
        metavar="PATH",
        help=(
            "Plain-text GEPA merge template containing <gepa_generated>. "
            "If set, reflection fills that region only (partial merge). If omitted, full policy is rewritten each iteration."
        ),
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
        timestamp = datetime.now().strftime("%m-%d_%H-%M-%S")
        log_dir = f"{base_dir}_{timestamp}"
        print(f"Fresh run: {log_dir}")
    else:
        log_dir = base_dir

    os.makedirs(log_dir, exist_ok=True)

    if args.seed_candidate_file is not None:
        seed_path = Path(args.seed_candidate_file).expanduser().resolve()
        if not seed_path.is_file():
            raise FileNotFoundError(f"Seed candidate file not found: {seed_path}")
        seed_candidate = seed_path.read_text(encoding="utf-8")
        seed_candidate_file_label = str(seed_path)
    else:
        seed_candidate = load_policy_solo_seed()
        seed_candidate_file_label = str(resolve_policy_solo_seed_path().resolve())

    objective, background, reflection_prompts_label, optimizer, evaluator_prompt_template = _resolve_reflection_prompts(
        args.reflection_prompts_file
    )

    gepa_template_file_label: str | None = None
    if args.gepa_template_file:
        gepa_generated_template = load_gepa_template_file(args.gepa_template_file)
        gepa_template_file_label = str(Path(args.gepa_template_file).expanduser().resolve())
        gepa_reflection_style = f"partial (template file: {Path(gepa_template_file_label).name})"
    else:
        gepa_generated_template = None
        gepa_reflection_style = "full_candidate (no --gepa-template-file)"

    # Train-only: fixed task IDs, no valset
    train_set = load_tau2_retail_train_only()
    # train_set, val_set = load_tau2_retail_dataset()
    print(f"Dataset: train={len(train_set)} (no valset)")
    print(f"Seed policy file: {seed_candidate_file_label}")
    if reflection_prompts_label:
        print(f"Reflection prompts file: {reflection_prompts_label}")
    else:
        print("Reflection prompts: inline defaults (utils constants; no markdown file)")
    print(f"GEPA merge (candidate): {gepa_reflection_style}")

    optimizer_body = (optimizer or "").strip()
    if optimizer_body:
        custom_reflection_template = build_reflection_prompt_from_optimizer_template(
            optimizer_body,
            objective=objective,
            background=background,
        )
        reflection_for_optimize = (None, None)
        print("Reflection prompt: custom (# Optimizer template)")
    else:
        custom_reflection_template = None
        reflection_for_optimize = (objective, background)
        print("Reflection prompt: GEPA _build_reflection_prompt_template (objective/background)")

    def evaluator(candidate: str, example) -> tuple[float, SideInfo]:
        return evaluate(
            candidate,
            example,
            llm_agent=args.llm_agent,
            seed=args.seed,
            diagnosis_lm=args.reflection_lm,
            diagnosis_prompt_template=evaluator_prompt_template,
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
            seed_candidate_file=seed_candidate_file_label,
            reflection_prompts_file=reflection_prompts_label,
            gepa_template_file=gepa_template_file_label,
        ),
        reflection=ReflectionConfig(
            reflection_lm=args.reflection_lm,
            reflection_minibatch_size=10,
            gepa_generated_template=gepa_generated_template,
            **(
                {"reflection_prompt_template": custom_reflection_template}
                if custom_reflection_template is not None
                else {}
            ),
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
        objective=reflection_for_optimize[0],
        background=reflection_for_optimize[1],
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
