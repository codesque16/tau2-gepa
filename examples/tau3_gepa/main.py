#!/usr/bin/env python3
"""GEPA retail optimization using **τ³-bench-fork** text simulations (YAML + ``tau2config`` parity).

Executor calls :func:`tau2.gepa_runner.run_gepa_evaluation_task` (same merge semantics as
``uv run tau2config --config … --run-ids …``) for each dataset example. Reflection uses the
same ``tau2_retail`` reflective row shape as ``tau2_retail_mermaid``; stack Logfire spans
``Task:{id}`` / ``simulation`` / ``evaluation`` / ``gepa_eval`` like the mermaid example.

``gepa.llm_seed_chain`` uses :mod:`domains.retail.llm_seed_chain` (same as ``tau2_retail_mermaid``).
``gepa.split_file_path`` may point at τ³ JSON with ``train``/``val`` (or ``train_task_ids``/``val_task_ids``);
optional ``gepa.split_train_key`` / ``gepa.split_val_key`` select columns (e.g. ``test`` as held-out).

Run from repo root::

  uv sync
  uv run python gepa/examples/tau3_gepa/main.py --config configs/gepa_tau3_retail.yaml --fresh
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_EXAMPLE_FILE = Path(__file__).resolve()
_GEPA_DIR = _EXAMPLE_FILE.parents[2]
REPO_ROOT = _EXAMPLE_FILE.parents[3]
TAU3_ROOT = REPO_ROOT / "tau3-bench-fork"
for _p in (_GEPA_DIR, REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from examples.tau3_gepa.reflection_prompts_md import (
    build_reflection_prompt_from_optimizer_template,
    load_gepa_template_file,
    load_reflection_prompts_file,
    validate_diagnosis_prompt_template,
)
from examples.tau3_gepa.utils import (
    BACKGROUND,
    OBJECTIVE_TRAIN_ONLY,
    evaluate_policy_with_tau3_simulation,
    load_tau3_tasks_as_dicts,
    merge_tau3_yaml_defaults,
    tau3_fork_roots,
)
from gepa.optimize_anything import (
    EngineConfig,
    GEPAConfig,
    MergeConfig,
    ReflectionConfig,
    SideInfo,
    TrackingConfig,
    optimize_anything,
)
from gepa.strategies.kfold_rotation_eval_policy import KFoldRotationEvaluationPolicy
from gepa.strategies.stratified_kfold_sampler import StratifiedKFoldBatchSampler

_EXAMPLE_DIR = _EXAMPLE_FILE.parent
_REFLECTION_PROMPTS_FALLBACK_CHAIN = (
    _EXAMPLE_DIR / "reflection_prompts_mermaid.md",
    _EXAMPLE_DIR.parent / "tau2_retail_mermaid" / "reflection_prompts_mermaid.md",
)
_DEFAULT_CONFIG = REPO_ROOT / "configs" / "gepa_tau3_retail.yaml"


def _filtered_dataclass(cls: type[Any], **kwargs: Any) -> Any:
    allowed = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in kwargs.items() if k in allowed})


def _resolve_repo_path(repo_root: Path, rel_or_abs: str | Path) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else repo_root / p


def _normalize_gepa_optimization_mode(raw: str | None) -> str:
    m = str(raw or "single_task").strip().lower().replace("-", "_")
    aliases = {
        "single": "single_task",
        "single_task": "single_task",
        "multi": "multi_task",
        "multi_task": "multi_task",
        "generalization": "generalization",
        "generalise": "generalization",
        "generalize": "generalization",
    }
    out = aliases.get(m)
    if out is None:
        raise ValueError(
            "gepa.optimization_mode must be single_task, multi_task, or generalization "
            f"(got {raw!r})."
        )
    return out


def _load_split_ids_from_file(
    repo_root: Path,
    tau3_root: Path,
    gepa_cfg: dict[str, Any],
) -> tuple[list[str] | None, list[str] | None]:
    """Load train/val task id lists for generalization.

    Supports:

    - GEPA autosplit style: ``train_task_ids`` / ``val_task_ids``
    - τ³ split JSON (e.g. ``split_tasks_60_20_20.json``): ``train`` / ``val`` /
      ``test`` — keys configurable via ``gepa.split_train_key`` /
      ``gepa.split_val_key`` (defaults ``train`` / ``val``).
    """
    split_file = gepa_cfg.get("split_file_path")
    if not split_file:
        return None, None
    rel = str(split_file)
    path = _resolve_repo_path(repo_root, rel)
    if not path.is_file():
        path = _resolve_repo_path(tau3_root, rel)
    if not path.is_file():
        path = Path(rel).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"gepa.split_file_path not found: {split_file}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} root must be a JSON object")

    train_key = str(gepa_cfg.get("split_train_key") or "train").strip() or "train"
    val_key = str(gepa_cfg.get("split_val_key") or "val").strip() or "val"

    if payload.get("train_task_ids") is not None:
        train_ids = [str(x) for x in (payload.get("train_task_ids") or [])]
    elif train_key in payload and payload[train_key] is not None:
        train_ids = [str(x) for x in payload[train_key]]
    else:
        raise ValueError(
            f"{path}: expected non-empty 'train_task_ids' or {train_key!r} list "
            f"(set gepa.split_train_key to match your JSON)."
        )

    if payload.get("val_task_ids") is not None:
        val_ids = [str(x) for x in (payload.get("val_task_ids") or [])]
    elif val_key in payload and payload[val_key] is not None:
        val_ids = [str(x) for x in payload[val_key]]
    else:
        raise ValueError(
            f"{path}: expected non-empty 'val_task_ids' or {val_key!r} list "
            f"(set gepa.split_val_key to use e.g. 'test' as held-out)."
        )

    if not train_ids:
        raise ValueError(f"{path}: train id list is empty.")
    if not val_ids:
        raise ValueError(f"{path}: val id list is empty.")

    overlap = set(train_ids) & set(val_ids)
    if overlap:
        raise ValueError(f"{path}: train and val splits overlap: {sorted(overlap)[:20]}…")

    return train_ids, val_ids


def _parse_max_metric_calls(gepa: dict[str, Any]) -> int | None:
    if "max_metric_calls" not in gepa:
        return 200
    raw = gepa["max_metric_calls"]
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("gepa.max_metric_calls must be an integer or null, not a boolean")
    return int(raw)


def _task_id_str(task: dict[str, Any]) -> str:
    return str(task.get("id"))


def _load_pass_fail_table(path: Path) -> dict[str, bool]:
    """Return map task_id -> failed(bool). Accepts several JSON layouts."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, bool] = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            sv = str(v).strip().lower()
            out[str(k)] = sv in ("fail", "failed", "0", "false", "f")
        return out
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            tid = row.get("id", row.get("task_id"))
            if tid is None:
                continue
            v = row.get("status", row.get("result", row.get("pass_fail", row.get("passed"))))
            if isinstance(v, bool):
                failed = not v
            else:
                sv = str(v).strip().lower()
                failed = sv in ("fail", "failed", "0", "false", "f")
            out[str(tid)] = failed
        return out
    raise ValueError(f"Unsupported pass/fail table format at {path}")


def _stratified_train_test_split(
    all_task_ids: list[str], failed_by_id: dict[str, bool], test_fraction: float, rng: random.Random
) -> tuple[set[str], set[str]]:
    fail_ids = [x for x in all_task_ids if failed_by_id.get(x, False)]
    succ_ids = [x for x in all_task_ids if not failed_by_id.get(x, False)]
    rng.shuffle(fail_ids)
    rng.shuffle(succ_ids)
    n_fail_test = int(round(len(fail_ids) * test_fraction))
    n_succ_test = int(round(len(succ_ids) * test_fraction))
    test_ids = set(fail_ids[:n_fail_test] + succ_ids[:n_succ_test])
    train_ids = set(all_task_ids) - test_ids
    if not train_ids or not test_ids:
        raise ValueError("cv_generalization split produced empty train or test set.")
    return train_ids, test_ids


def _make_stratified_folds(
    train_index_by_task_id: dict[str, int], failed_by_id: dict[str, bool], k: int, rng: random.Random
) -> list[list[int]]:
    fail_idx = [idx for tid, idx in train_index_by_task_id.items() if failed_by_id.get(tid, False)]
    succ_idx = [idx for tid, idx in train_index_by_task_id.items() if not failed_by_id.get(tid, False)]
    if k < 2:
        raise ValueError("cv_generalization.k_folds must be >= 2.")
    rng.shuffle(fail_idx)
    rng.shuffle(succ_idx)
    folds: list[list[int]] = [[] for _ in range(k)]
    for i, idx in enumerate(fail_idx):
        folds[i % k].append(idx)
    for i, idx in enumerate(succ_idx):
        folds[i % k].append(idx)
    for f in folds:
        if not f:
            raise ValueError("cv_generalization produced an empty validation fold; reduce k_folds.")
        rng.shuffle(f)
    return folds


def _resolve_reflection_prompts(
    path_arg: str | None,
) -> tuple[str, str, str | None, str | None, str | None]:
    if path_arg is None:
        for candidate in _REFLECTION_PROMPTS_FALLBACK_CHAIN:
            if candidate.is_file():
                p = candidate.resolve()
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


def main() -> None:
    repo_root = REPO_ROOT
    tau3_fork_roots()

    parser = argparse.ArgumentParser(
        description="Tau3 retail GEPA (τ³-bench-fork YAML runs) — config-driven + CLI overrides"
    )
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--run-dir", default=None, help="Resume GEPA run directory")
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Optional parent Logfire span + output leaf directory name",
    )
    args = parser.parse_args()

    os.chdir(TAU3_ROOT)

    cfg_path = args.config if args.config.is_absolute() else repo_root / args.config
    merged, _ = merge_tau3_yaml_defaults(repo_root, cfg_path)
    tau3 = merged.get("tau3") or {}

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(TAU3_ROOT / ".env")

    gepa = merged.get("gepa") or {}
    if not gepa:
        raise ValueError(f"YAML must contain a non-empty 'gepa:' section: {cfg_path}")

    yaml_rel = tau3.get("yaml_config") or "examples/retail_vertex_text.yaml"
    tau3_yaml = _resolve_repo_path(TAU3_ROOT, yaml_rel)
    if not tau3_yaml.is_file():
        tau3_yaml = _resolve_repo_path(repo_root, yaml_rel)
    if not tau3_yaml.is_file():
        raise FileNotFoundError(f"tau3.yaml_config not found: {yaml_rel}")

    tau3_run_id = str(tau3.get("run_id") or "retail_conv_full_gemma4_26b_fp8_cloud_run")
    merged_overrides = tau3.get("merged_overrides") if isinstance(tau3.get("merged_overrides"), dict) else None
    verbose_tau3 = bool(tau3.get("verbose_logs", False))
    evaluate_comm = bool(gepa.get("evaluate_communication", True))

    engine_seed = int(gepa.get("engine_seed", 0))
    opt_mode = _normalize_gepa_optimization_mode(gepa.get("optimization_mode"))

    split_dataset_ids, split_valset_ids = _load_split_ids_from_file(repo_root, TAU3_ROOT, gepa)

    dataset_tasks: list[dict[str, Any]] | None = None
    valset_tasks: list[dict[str, Any]] | None = None
    single_task: dict[str, Any] | None = None
    cv_eval_policy: KFoldRotationEvaluationPolicy | None = None
    strat_sampler: StratifiedKFoldBatchSampler | None = None

    task_set = str(tau3.get("task_set_name") or "retail")
    if "task_split_name" not in tau3:
        task_split: str | None = "base"
    else:
        _ts = tau3.get("task_split_name")
        if _ts is None or (isinstance(_ts, str) and _ts.strip().lower() in ("", "null", "none")):
            task_split = None
        else:
            task_split = str(_ts).strip()

    if opt_mode == "single_task":
        tid = gepa.get("task_id")
        if tid is None:
            raise ValueError("gepa.task_id is required when gepa.optimization_mode is single_task.")
        loaded = load_tau3_tasks_as_dicts(
            task_ids=[str(tid)], task_set_name=task_set, task_split_name=task_split
        )
        if len(loaded) != 1:
            raise RuntimeError(f"Expected one τ³ task for gepa.task_id={tid!r}; got {len(loaded)}.")
        single_task = loaded[0]
    elif opt_mode == "multi_task":
        d_ids = split_dataset_ids if split_dataset_ids is not None else gepa.get("dataset_task_ids")
        if not d_ids:
            raise ValueError("gepa.dataset_task_ids required for multi_task.")
        ds_ids = [str(x) for x in d_ids]
        dataset_tasks = load_tau3_tasks_as_dicts(
            task_ids=ds_ids, task_set_name=task_set, task_split_name=task_split
        )
        if not dataset_tasks:
            raise RuntimeError("No τ³ tasks loaded for dataset_task_ids.")
    else:
        d_ids = split_dataset_ids if split_dataset_ids is not None else gepa.get("dataset_task_ids")
        v_ids = split_valset_ids if split_valset_ids is not None else gepa.get("valset_task_ids")
        if not d_ids or not v_ids:
            raise ValueError("gepa.dataset_task_ids and valset_task_ids required for generalization.")
        ds_ids = [str(x) for x in d_ids]
        vs_ids = [str(x) for x in v_ids]
        overlap = set(ds_ids) & set(vs_ids)
        if overlap:
            raise ValueError(f"dataset and valset must be disjoint; overlap={sorted(overlap)}")
        dataset_tasks = load_tau3_tasks_as_dicts(
            task_ids=ds_ids, task_set_name=task_set, task_split_name=task_split
        )
        valset_tasks = load_tau3_tasks_as_dicts(
            task_ids=vs_ids, task_set_name=task_set, task_split_name=task_split
        )
        if not dataset_tasks or not valset_tasks:
            raise RuntimeError("generalization: failed to load dataset or valset tasks.")

        # Optional: dynamic train/val CV inside generalization mode from a pass/fail table.
        cv_cfg = gepa.get("cv_generalization") if isinstance(gepa.get("cv_generalization"), dict) else None
        if cv_cfg and bool(cv_cfg.get("enabled", False)):
            pf_rel = cv_cfg.get("pass_fail_table")
            if not pf_rel:
                raise ValueError("gepa.cv_generalization.enabled=true requires pass_fail_table.")
            pf_path = _resolve_repo_path(repo_root, str(pf_rel))
            if not pf_path.is_file():
                raise FileNotFoundError(f"cv_generalization.pass_fail_table not found: {pf_path}")
            failed_by_id = _load_pass_fail_table(pf_path)

            # Build one pooled set first, then stratified train/test split.
            pooled = list(dataset_tasks) + list(valset_tasks)
            pooled_ids = [_task_id_str(x) for x in pooled]
            split_seed = int(cv_cfg.get("seed", engine_seed))
            split_rng = random.Random(split_seed)
            test_fraction = float(cv_cfg.get("test_fraction", 0.2))
            train_id_set, test_id_set = _stratified_train_test_split(pooled_ids, failed_by_id, test_fraction, split_rng)
            train_tasks = [x for x in pooled if _task_id_str(x) in train_id_set]
            test_tasks = [x for x in pooled if _task_id_str(x) in test_id_set]
            if not train_tasks or not test_tasks:
                raise RuntimeError("cv_generalization produced empty train/test tasks.")

            train_id_to_idx = {_task_id_str(t): i for i, t in enumerate(train_tasks)}
            fail_train_idx = {i for i, t in enumerate(train_tasks) if failed_by_id.get(_task_id_str(t), False)}
            succ_train_idx = set(range(len(train_tasks))) - fail_train_idx
            if not fail_train_idx or not succ_train_idx:
                raise RuntimeError("cv_generalization train split must contain both failure and success examples.")

            k_folds = int(cv_cfg.get("k_folds", 5))
            folds = _make_stratified_folds(train_id_to_idx, failed_by_id, k_folds, split_rng)
            cv_eval_policy = KFoldRotationEvaluationPolicy(folds)
            minibatch_failures = cv_cfg.get("minibatch_failures")
            if minibatch_failures is not None:
                minibatch_failures = int(minibatch_failures)

            dataset_tasks = train_tasks
            valset_tasks = train_tasks
            # Keep held-out test for external reporting only.
            test_fail = sum(1 for t in test_tasks if failed_by_id.get(_task_id_str(t), False))
            train_fail = len(fail_train_idx)
            print(
                "cv_generalization: "
                f"train={len(train_tasks)} (fail={train_fail}, success={len(train_tasks)-train_fail}) | "
                f"test={len(test_tasks)} (fail={test_fail}, success={len(test_tasks)-test_fail}) | "
                f"k_folds={k_folds}"
            )
            strat_sampler = StratifiedKFoldBatchSampler(
                minibatch_size=max(1, int(gepa.get("reflection_minibatch_size") or 1)),
                rng=split_rng,
                failure_ids=fail_train_idx,
                success_ids=succ_train_idx,
                val_folds=folds,
                failure_quota=minibatch_failures,
            )
        else:
            strat_sampler = None

    seed_pol = gepa.get("seed_policy_path")
    if not seed_pol:
        raise ValueError("gepa.seed_policy_path is required.")
    seed_path = _resolve_repo_path(repo_root, str(seed_pol))
    if not seed_path.is_file():
        raise FileNotFoundError(f"gepa.seed_policy_path not found: {seed_path}")
    seed_candidate = seed_path.read_text(encoding="utf-8")
    seed_label = str(seed_path)

    out_base = str(gepa.get("output_dir") or "outputs/tau3_gepa")
    experiment_name = (str(args.experiment_name).strip() if args.experiment_name else None) or None
    fresh = bool(args.fresh or gepa.get("fresh"))
    if args.run_dir is not None:
        log_dir = str(Path(args.run_dir).expanduser())
    elif experiment_name is not None:
        log_dir = str(Path(out_base).expanduser().parent / experiment_name)
    elif gepa.get("run_dir"):
        log_dir = str(_resolve_repo_path(repo_root, str(gepa["run_dir"])))
    elif fresh:
        log_dir = f"{out_base.rstrip('/')}_{datetime.now().strftime('%m-%d_%H-%M-%S')}"
    else:
        log_dir = out_base

    os.makedirs(log_dir, exist_ok=True)
    artifact_root = Path(log_dir) / "tau3_gepa_artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    refl_arg = gepa.get("reflection_prompts_file")
    if refl_arg in (None, "", "null"):
        objective, background, reflection_label, optimizer, evaluator_prompt_template = _resolve_reflection_prompts(
            None
        )
    else:
        objective, background, reflection_label, optimizer, evaluator_prompt_template = (
            _resolve_reflection_prompts(str(_resolve_repo_path(repo_root, str(refl_arg))))
        )

    tmpl = gepa.get("gepa_template_file")
    gepa_template_label: str | None = None
    if tmpl in (None, "", "null"):
        gepa_generated_template = None
    else:
        tmpl_path = _resolve_repo_path(repo_root, str(tmpl))
        gepa_generated_template = load_gepa_template_file(str(tmpl_path))
        gepa_template_label = str(tmpl_path)

    optimizer_body = (optimizer or "").strip()
    if optimizer_body:
        custom_reflection_template = build_reflection_prompt_from_optimizer_template(
            optimizer_body,
            objective=objective,
            background=background,
        )
        reflection_for_optimize = (None, None)
    else:
        custom_reflection_template = None
        reflection_for_optimize = (objective, background)

    eval_seed_raw = gepa.get("evaluation_seed")
    eval_seed: int | None
    if eval_seed_raw is None or str(eval_seed_raw).strip() == "":
        eval_seed = None
    else:
        eval_seed = int(eval_seed_raw)

    _gepa_cb_allowed = "gepa_callbacks" in {f.name for f in dataclasses.fields(GEPAConfig)}
    use_llm_seed_chain = bool(gepa.get("llm_seed_chain", False))
    llm_seed_chain: Any = None
    gepa_callbacks_list: list[Any] | None = None
    if use_llm_seed_chain:
        from domains.retail.llm_seed_chain import (
            LLMSeedChain,
            LLMSeedChainCallback,
            retail_llm_seed_master_from_gepa_cfg,
        )

        if not _gepa_cb_allowed:
            print(
                "WARNING: gepa.llm_seed_chain is true but GEPAConfig has no gepa_callbacks; "
                "seed chaining disabled. Use the repo's vendored gepa.",
                file=sys.stderr,
            )
        else:
            _master = retail_llm_seed_master_from_gepa_cfg(gepa)
            llm_seed_chain = LLMSeedChain(_master)
            gepa_callbacks_list = [LLMSeedChainCallback(llm_seed_chain)]

    reflection_lm_spec = gepa.get("reflection_lm") or "gemini/gemini-3-flash-preview"
    reflection_llm_backend = str(gepa.get("reflection_llm_backend") or "litellm").strip().lower()
    if reflection_llm_backend not in ("litellm", "genai", "openai", "anthropic"):
        raise ValueError(f"Unsupported gepa.reflection_llm_backend: {reflection_llm_backend!r}")

    _dbd = gepa.get("diagnosis_llm_backend")
    diagnosis_llm_backend = (
        str(_dbd).strip().lower()
        if _dbd not in (None, "", "null")
        else reflection_llm_backend
    )
    if diagnosis_llm_backend not in ("litellm", "genai", "openai", "anthropic"):
        raise ValueError(f"Unsupported gepa.diagnosis_llm_backend: {diagnosis_llm_backend!r}")

    _diag = gepa.get("diagnosis_lm")
    diagnosis_lm = str(_diag).strip() if _diag is not None and str(_diag).strip() else None
    if diagnosis_lm and diagnosis_llm_backend == "genai" and diagnosis_lm.startswith("gemini/"):
        diagnosis_lm = diagnosis_lm.split("/", 1)[1].strip()

    _dgt = gepa.get("diagnosis_genai_temperature")
    diagnosis_genai_temperature = float(_dgt) if _dgt is not None else None
    _dgmo = gepa.get("diagnosis_genai_max_output_tokens")
    diagnosis_genai_max_output_tokens = int(_dgmo) if _dgmo is not None else None
    diagnosis_genai_reasoning_effort = str(gepa.get("diagnosis_genai_reasoning_effort") or "").strip() or None
    diagnosis_genai_vertex_ai = bool(gepa.get("diagnosis_genai_vertex_ai", False))

    _eval_tpl_stripped = (evaluator_prompt_template or "").strip() or None
    if _eval_tpl_stripped and diagnosis_lm:
        validate_diagnosis_prompt_template(_eval_tpl_stripped)

    max_metric = _parse_max_metric_calls(gepa)
    max_workers = max(1, int(gepa.get("max_workers") or 2))
    rfmt = str(gepa.get("reflective_dataset_format") or "tau2_retail").lower()
    if rfmt not in ("default", "tau2_retail"):
        raise ValueError("gepa.reflective_dataset_format must be 'default' or 'tau2_retail'")

    _ec_names = {f.name for f in dataclasses.fields(EngineConfig)}
    if rfmt == "tau2_retail" and "reflective_dataset_format" not in _ec_names:
        print("Note: EngineConfig.reflective_dataset_format missing; using default adapter rows.", file=sys.stderr)

    mb = gepa.get("reflection_minibatch_size")
    _n_reflect = len(dataset_tasks) if dataset_tasks else 1
    minibatch = min(10, max(1, _n_reflect)) if mb is None else max(1, int(mb))

    use_cloudpickle = bool(gepa.get("use_cloudpickle", False))
    if reflection_llm_backend == "genai":
        from agent.genai_gepa_lm import make_genai_gepa_lm

        mid = str(reflection_lm_spec).strip()
        if mid.startswith("gemini/"):
            mid = mid.split("/", 1)[1].strip()
        _r_temp = gepa.get("reflection_genai_temperature")
        reflection_lm: Any = make_genai_gepa_lm(
            mid,
            temperature=float(_r_temp) if _r_temp is not None else None,
            max_output_tokens=int(gepa["reflection_genai_max_output_tokens"])
            if gepa.get("reflection_genai_max_output_tokens") is not None
            else None,
            reasoning_effort=str(gepa.get("reflection_genai_reasoning_effort") or "").strip() or None,
            vertex_ai=bool(gepa.get("reflection_genai_vertex_ai", False)),
        )
        if not use_cloudpickle:
            print(
                "Note: reflection_llm_backend genai uses a callable; enable gepa.use_cloudpickle for checkpoint.",
                file=sys.stderr,
            )
    elif reflection_llm_backend == "openai":
        from agent.openai_gepa_lm import make_openai_gepa_lm

        mid = str(reflection_lm_spec).strip()
        if mid.startswith("openai/"):
            mid = mid.split("/", 1)[1].strip()
        _r_temp = gepa.get("reflection_genai_temperature")
        reflection_lm = make_openai_gepa_lm(
            mid,
            temperature=float(_r_temp) if _r_temp is not None else None,
            max_tokens=int(gepa["reflection_genai_max_output_tokens"])
            if gepa.get("reflection_genai_max_output_tokens") is not None
            else None,
            reasoning_effort=str(gepa.get("reflection_genai_reasoning_effort") or "").strip() or None,
        )
        if not use_cloudpickle:
            print("Note: enable gepa.use_cloudpickle for checkpoint with openai backend.", file=sys.stderr)
    elif reflection_llm_backend == "anthropic":
        from agent.anthropic_gepa_lm import make_anthropic_gepa_lm

        mid = str(reflection_lm_spec).strip()
        if mid.startswith("anthropic/"):
            mid = mid.split("/", 1)[1].strip()
        _r_temp = gepa.get("reflection_genai_temperature")
        reflection_lm = make_anthropic_gepa_lm(
            mid,
            temperature=float(_r_temp) if _r_temp is not None else None,
            max_tokens=int(gepa["reflection_genai_max_output_tokens"])
            if gepa.get("reflection_genai_max_output_tokens") is not None
            else None,
            reasoning_effort=str(gepa.get("reflection_genai_reasoning_effort") or "").strip() or None,
        )
        if not use_cloudpickle:
            print("Note: enable gepa.use_cloudpickle for checkpoint with anthropic backend.", file=sys.stderr)
    else:
        reflection_lm = str(reflection_lm_spec)

    def _eval_common_kwargs() -> dict[str, Any]:
        return {
            "tau3_yaml_path": tau3_yaml,
            "tau3_run_id": tau3_run_id,
            "seed": llm_seed_chain.read() if llm_seed_chain is not None else eval_seed,
            "gepa_artifact_root": artifact_root,
            "diagnosis_lm": diagnosis_lm,
            "diagnosis_prompt_template": _eval_tpl_stripped,
            "diagnosis_llm_backend": diagnosis_llm_backend,
            "diagnosis_genai_temperature": diagnosis_genai_temperature,
            "diagnosis_genai_max_output_tokens": diagnosis_genai_max_output_tokens,
            "diagnosis_genai_reasoning_effort": diagnosis_genai_reasoning_effort,
            "diagnosis_genai_vertex_ai": diagnosis_genai_vertex_ai,
            "merged_overrides": merged_overrides,
            "evaluate_communication": evaluate_comm,
            "verbose_logs": verbose_tau3,
        }

    if opt_mode == "single_task":
        assert single_task is not None

        def evaluator(candidate: str | dict[str, Any]) -> tuple[float, SideInfo]:
            return evaluate_policy_with_tau3_simulation(
                candidate=candidate,
                example=single_task,
                **_eval_common_kwargs(),
            )
    else:
        assert dataset_tasks is not None

        def evaluator(candidate: str | dict[str, Any], example: dict[str, Any]) -> tuple[float, SideInfo]:
            return evaluate_policy_with_tau3_simulation(
                candidate=candidate,
                example=example,
                **_eval_common_kwargs(),
            )

    reflection_kw: dict[str, Any] = {
        "reflection_lm": reflection_lm,
        "reflection_minibatch_size": minibatch,
        "gepa_generated_template": gepa_generated_template,
        "skip_perfect_score": bool(gepa.get("reflection_skip_perfect_score", False)),
        "batch_sampler": gepa.get("reflection_batch_sampler") or "epoch_shuffled",
        "module_selector": gepa.get("reflection_module_selector") or "round_robin",
    }
    _ps = gepa.get("reflection_perfect_score")
    if _ps is not None:
        reflection_kw["perfect_score"] = float(_ps)
    _mem = gepa.get("min_errors_minibatch")
    if _mem is not None:
        reflection_kw["min_errors_minibatch"] = int(_mem)
    _vms = gepa.get("vulnerable_minibatch_size")
    if _vms is not None:
        reflection_kw["vulnerable_minibatch_size"] = int(_vms)
    if strat_sampler is not None:
        reflection_kw["batch_sampler"] = strat_sampler
        _prov = getattr(strat_sampler, "_last_sample_provenance", None)
        if isinstance(_prov, dict):
            print(f"cv_generalization minibatch sampler: {_prov}")
    if custom_reflection_template is not None:
        reflection_kw["reflection_prompt_template"] = custom_reflection_template

    _mcp = gepa.get("max_candidate_proposals")
    max_candidate_proposals = int(_mcp) if _mcp is not None else None

    merge: MergeConfig | None = None
    if bool(gepa.get("merge_enabled", False)):
        merge = _filtered_dataclass(
            MergeConfig,
            max_merge_invocations=int(gepa.get("merge_max_invocations", 5)),
            merge_val_overlap_floor=int(gepa.get("merge_val_overlap_floor", 5)),
        )

    _top_gepa_kw: dict[str, Any] = {"merge": merge}
    if gepa_callbacks_list is not None:
        _top_gepa_kw["gepa_callbacks"] = gepa_callbacks_list

    config = GEPAConfig(
        engine=_filtered_dataclass(
            EngineConfig,
            run_dir=log_dir,
            max_metric_calls=max_metric,
            max_candidate_proposals=max_candidate_proposals,
            seed=engine_seed,
            parallel=bool(gepa.get("parallel", True)),
            max_workers=max_workers,
            use_cloudpickle=use_cloudpickle,
            raise_on_exception=bool(gepa.get("raise_on_exception", True)),
            cache_evaluation=bool(gepa.get("cache_evaluation", True)),
            cache_evaluation_storage=gepa.get("cache_evaluation_storage") or "auto",
            track_best_outputs=bool(gepa.get("track_best_outputs", True)),
            candidate_selection_strategy=gepa.get("candidate_selection_strategy") or "pareto",
            frontier_type=gepa.get("frontier_type") or "hybrid",
            val_evaluation_policy=cv_eval_policy or (gepa.get("val_evaluation_policy") or "full_eval"),
            best_example_evals_k=int(gepa.get("best_example_evals_k", 30)),
            display_progress_bar=bool(gepa.get("display_progress_bar", True)),
            capture_traces=bool(gepa.get("capture_traces", False)),
            capture_stdio=bool(gepa.get("capture_stdio", False)),
            seed_candidate_file=seed_label,
            reflection_prompts_file=reflection_label,
            gepa_template_file=gepa_template_label,
            reflective_dataset_format=rfmt,
        ),
        reflection=_filtered_dataclass(ReflectionConfig, **reflection_kw),
        tracking=_filtered_dataclass(
            TrackingConfig,
            use_wandb=bool(gepa.get("use_wandb", False)),
            use_logfire=bool(gepa.get("use_logfire", True)),
            dump_visualizer_events=bool(gepa.get("dump_visualizer_events", False)),
        ),
        **_top_gepa_kw,
    )

    want_lf = bool(gepa.get("use_logfire", True))
    if want_lf:
        try:
            import logfire
            from agent.logfire_gemini_integration import instrument_logfire_gemini
            from agent.telemetry import configure_logfire_tau2

            configure_logfire_tau2(
                scrubbing=False,
                console=False,
                use_gcp_trace=gepa.get("use_gcp_trace"),
            )
            instrument_logfire_gemini()
            logfire.instrument_litellm()
        except Exception as e:
            print(f"(Optional) Logfire skipped: {e}", file=sys.stderr)

    _logfire_for_span: Any = None
    try:
        import logfire as _lf

        _logfire_for_span = _lf
    except Exception:
        pass

    _cfg_name = cfg_path.name
    _top_span_label = f"tau3_gepa GEPA · {_cfg_name}"
    _lf_ok = want_lf and _logfire_for_span is not None
    top_span = (
        _logfire_for_span.span(
            _top_span_label,
            _span_name=_top_span_label,
            run_dir=log_dir,
            config_path=str(cfg_path.resolve()),
            tau3_yaml=str(tau3_yaml),
            tau3_run_id=tau3_run_id,
            optimization_mode=opt_mode,
        )
        if _lf_ok
        else contextlib.nullcontext()
    )

    parent_span = (
        _logfire_for_span.span(
            experiment_name,
            _span_name=experiment_name,
            experiment_name=experiment_name,
            output_dir=log_dir,
        )
        if experiment_name and _lf_ok
        else contextlib.nullcontext()
    )

    print(f"τ³ YAML: {tau3_yaml} run_id={tau3_run_id}")
    print(f"τ³ task_split_name: {task_split!r} (None = all tasks in set, then filter by id)")
    if split_dataset_ids is not None:
        print(
            f"Split file: {gepa.get('split_file_path')} → "
            f"train={len(split_dataset_ids)} val={len(split_valset_ids or [])}"
        )
    print(f"Log dir: {log_dir}")
    print(f"Artifacts: {artifact_root}")
    if use_llm_seed_chain and llm_seed_chain is not None:
        print(
            "llm_seed_chain: on "
            f"(master={retail_llm_seed_master_from_gepa_cfg(gepa)} from gepa.evaluation_seed, "
            "else gepa.seed, else 42)"
        )
    elif eval_seed is not None:
        print(f"Simulator LLM seed (fixed): evaluation_seed={eval_seed}")

    with parent_span:
        with top_span:
            if _lf_ok:
                try:
                    _logfire_for_span.info(
                        "experiment_config",
                        run_kind="gepa_tau3_gepa",
                        config_path=str(cfg_path.resolve()),
                        config_json=json.dumps(merged, default=str, sort_keys=True),
                    )
                except Exception:
                    pass

            if opt_mode == "single_task":
                dataset_arg = None
                valset_arg = None
            elif opt_mode == "multi_task":
                dataset_arg = dataset_tasks
                valset_arg = None
            else:
                dataset_arg = dataset_tasks
                valset_arg = valset_tasks

            result = optimize_anything(
                seed_candidate=seed_candidate,
                evaluator=evaluator,
                dataset=dataset_arg,
                valset=valset_arg,
                config=config,
                objective=reflection_for_optimize[0],
                background=reflection_for_optimize[1],
            )

    best = result.best_candidate
    best_score = result.val_aggregate_scores[result.best_idx]
    print(f"\nBest candidate score: {best_score:.4f}")
    out_path = Path(log_dir) / "best_policy.md"
    out_path.write_text(best or "", encoding="utf-8")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
