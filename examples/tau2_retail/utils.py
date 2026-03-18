"""Tau2 retail benchmark utilities: dataset loading and GEPA evaluation bridge."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# tau2 is an optional dependency: pip install -e /path/to/tau2-bench
try:
    from gepa.logging.eval_context import get_gepa_eval_context
    from tau2.gepa_eval import evaluate_for_gepa
    from tau2.utils.utils import DATA_DIR
except ImportError as e:
    raise ImportError(
        "tau2 package is required for the tau2_retail example. "
        "Install from tau2-bench: pip install -e /path/to/tau2-mermaid/tau2-bench"
    ) from e


# =============================================================================
# PROMPTS
# =============================================================================

BACKGROUND = """You are optimizing the retail agent policy (policy_solo.md) for a tau2 customer-service agent.

Tau2 retail domain:
- The agent handles customer service tasks (returns, exchanges, order modifications, etc.)
- Tasks are from retail_solo_comms: each task has communication requirements (communicate_info)
- The agent must complete the task AND satisfy communication criteria to get full reward
- Evaluation uses pass@1: fraction of tasks solved (reward >= 0.99)

Your candidate is the full policy document. The policy defines domain rules, action rules, and constraints.
The agent is given this policy as its domain knowledge; you are refining it for better task completion.

Common failure modes:
- Agent doesn't communicate required info to the user
- Agent gives up or times out before completing the task
- Agent makes incorrect policy assumptions
- Agent doesn't handle edge cases (e.g., partial refunds, exchange eligibility)
- Policy rules are ambiguous or missing for edge cases

Preserve the structure (markdown, sections) and improve clarity, completeness, and edge-case handling."""

OBJECTIVE = """Maximize the pass@1 score (fraction of retail tasks solved) on the tau2 benchmark."""

# Train-only mode: maximize score on this fixed set of task IDs (no valset).
TRAIN_ONLY_TASK_IDS = [
    "12", "17", "23", "27", "32", "33", "34", "45", "42", "43",
    "56", "57", "66", "68", "78", "73", "86", "81", "91", "113", "102", "103",
]
# Train-only mode: maximize score on this fixed set of task IDs (no valset).
TRAIN_ONLY_TASK_IDS = [
    "12", "23", "32", "56", "66", "78"
]
OBJECTIVE_TRAIN_ONLY = "Maximize the pass@1 score on the training set (no held-out valset)."


def load_policy_solo_seed(split_path: Path | None = None) -> str:
    """Load policy_solo.md content as the seed candidate."""
    if split_path is None:
        policy_path = DATA_DIR / "tau2" / "domains" / "retail" / "policy_solo.md"
    else:
        policy_path = Path(split_path).parent / "policy_solo.md"
    if not policy_path.exists():
        raise FileNotFoundError(
            f"policy_solo.md not found at {policy_path}. "
            "Set TAU2_DATA_DIR to point to tau2-bench/data."
        )
    return policy_path.read_text(encoding="utf-8")


# =============================================================================
# DATASET
# =============================================================================


@dataclass
class TaskExample:
    """Single task example for GEPA evaluation."""

    task_id: str


def load_tau2_retail_dataset(
    split_path: Path | None = None,
) -> tuple[list[TaskExample], list[TaskExample]]:
    """Load train and test task IDs from split_tasks.json.

    Returns (train_set, val_set) where val_set = test split.
    Uses tau2's DATA_DIR by default; override via split_path.
    """
    if split_path is None:
        split_path = DATA_DIR / "tau2" / "domains" / "retail" / "split_tasks.json"
    split_path = Path(split_path)

    if not split_path.exists():
        raise FileNotFoundError(
            f"split_tasks.json not found at {split_path}. "
            "Set TAU2_DATA_DIR to point to tau2-bench/data, or pass split_path."
        )

    with open(split_path) as f:
        splits = json.load(f)

    train_ids = splits.get("train", [])
    test_ids = splits.get("test", [])
    # Prefer specific task IDs for reproducibility; only use IDs present in test split.
    # Split file uses string IDs (e.g. "12", "39").
    preferred_val_ids = ["12", "39"]
    val_task_ids = [tid for tid in preferred_val_ids if tid in test_ids]
    if not val_task_ids:
        val_task_ids = test_ids[:2] if len(test_ids) >= 2 else test_ids

    train_set = [TaskExample(task_id=tid) for tid in train_ids]
    val_set = [TaskExample(task_id=tid) for tid in val_task_ids]

    return train_set, val_set


def load_tau2_retail_train_only(
    task_ids: list[str] | None = None,
) -> list[TaskExample]:
    """Load only a fixed list of task IDs as the training set (no valset).

    Use for train-only optimization: objective is to maximize score on this set.
    """
    ids = task_ids if task_ids is not None else TRAIN_ONLY_TASK_IDS
    return [TaskExample(task_id=tid) for tid in ids]


# =============================================================================
# EVALUATOR
# =============================================================================


def evaluate(
    candidate: str,
    example: TaskExample,
    domain: str = "retail",
    task_set_name: str = "retail_solo_comms",
    agent: str = "llm_agent_solo2",
    llm_agent: str = "gpt-5-mini",
    max_steps: int = 60,
    num_trials: int = 1,
    seed: int = 7789797979,
    policy_override: bool = True,
    diagnosis_lm: str | None = None,
) -> tuple[float, dict[str, Any]]:
    """Evaluate a candidate (policy or extra instructions) on a single tau2 task.

    When policy_override=True, candidate is the full policy content (policy_solo.md).
    When policy_override=False, candidate is extra instructions appended to agent prompt.

    Returns (score, side_info) for GEPA optimize_anything.
    """
    gepa_context = get_gepa_eval_context()
    score, feedback = evaluate_for_gepa(
        task_ids=[example.task_id],
        agent_extra_instructions="" if policy_override else candidate,
        policy_override=candidate if policy_override else None,
        domain=domain,
        task_set_name=task_set_name,
        agent=agent,
        llm_agent=llm_agent,
        max_steps=max_steps,
        num_trials=num_trials,
        seed=seed,
        log_level="WARNING",
        diagnosis_lm=diagnosis_lm,
        gepa_context=gepa_context,
    )

    side_info: dict[str, Any] = {
        "score": score,
        "task_id": example.task_id,
        "candidate_preview": candidate[:800] + ("..." if len(candidate) > 800 else ""),
        **feedback,
    }

    return score, side_info


def evaluate_on_dataset(
    candidate: str,
    dataset: list[TaskExample],
    **kwargs: Any,
) -> float:
    """Evaluate a candidate on a full dataset. Returns average pass@1."""
    if not dataset:
        return 0.0

    total = 0.0
    for ex in dataset:
        score, _ = evaluate(candidate, ex, **kwargs)
        total += score

    return total / len(dataset)
