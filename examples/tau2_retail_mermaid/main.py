#!/usr/bin/env python3
"""Tau2 retail GEPA using the **tau2-mermaid** solo agent (e.g. Gemini + MCP), not tau2-bench.

Configuration is **YAML-driven**; see ``configs/gepa_retail_mermaid.yaml``. ``gepa.optimization_mode`` follows
:class:`gepa.optimize_anything.optimize_anything`: **single_task** (``dataset=None``, ``valset=None``),
**multi_task** (``dataset`` only), **generalization** (``dataset`` + ``valset``).

Requires the **tau2-mermaid** monorepo. Use **``uv sync``** so ``gepa`` resolves to the
vendored ``./gepa`` package (``[tool.uv.sources]``): PyPI-only ``gepa`` omits
``TrackingConfig.use_logfire``, so **nested Logfire spans** from ``LogfireSpanCallback``
never register (unlike ``examples/tau2_retail/main.py`` with a full GEPA install).

Run from repo root::

  uv sync
  uv run python gepa/examples/tau2_retail_mermaid/main.py --config configs/gepa_retail_mermaid.yaml --fresh

CLI overrides: ``--config``, ``--fresh``, ``--run-dir`` (resume path).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_EXAMPLE_FILE = Path(__file__).resolve()
_GEPA_DIR = _EXAMPLE_FILE.parents[2]
REPO_ROOT = _EXAMPLE_FILE.parents[3]
for _p in (_GEPA_DIR, REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
if not (REPO_ROOT / "domains" / "retail" / "evaluate.py").is_file():
    sys.stderr.write(
        f"error: expected tau2-mermaid checkout (missing {REPO_ROOT / 'domains' / 'retail' / 'evaluate.py'})\n"
    )
    raise SystemExit(1)

from examples.tau2_retail_mermaid.reflection_prompts_md import (
    build_reflection_prompt_from_optimizer_template,
    load_gepa_template_file,
    load_reflection_prompts_file,
    validate_diagnosis_prompt_template,
)
from examples.tau2_retail_mermaid.utils import (
    BACKGROUND,
    OBJECTIVE_TRAIN_ONLY,
    domain_evaluate_communication_from_raw,
    evaluate_policy_with_mermaid_agent,
    load_retail_tasks_json,
    load_stacked_yaml,
    simulation_dict_for_solo,
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

_EXAMPLE_DIR = _EXAMPLE_FILE.parent
_REFLECTION_PROMPTS_FALLBACK_CHAIN = (
    _EXAMPLE_DIR / "reflection_prompts_mermaid.md",
    _EXAMPLE_DIR / "reflection_prompts_solo_v1.md",
    _EXAMPLE_DIR / "reflection_prompts.md",
)
_DEFAULT_CONFIG = REPO_ROOT / "configs" / "gepa_retail_mermaid.yaml"


def _filtered_dataclass(cls: type[Any], **kwargs: Any) -> Any:
    """Instantiate a dataclass, dropping kwargs the installed ``gepa`` build does not define."""
    allowed = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in kwargs.items() if k in allowed})


def _resolve_repo_path(repo_root: Path, rel_or_abs: str | Path) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else repo_root / p


def _normalize_gepa_optimization_mode(raw: str | None) -> str:
    """Map YAML to one of ``single_task`` | ``multi_task`` | ``generalization`` (:func:`optimize_anything`)."""
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
            f"(got {raw!r}). Matches :func:`gepa.optimize_anything.optimize_anything` "
            "(dataset/valset: none / dataset only / dataset + valset)."
        )
    return out


def _load_split_ids_from_file(repo_root: Path, gepa_cfg: dict[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    """Load train/val IDs from generated autosplit JSON."""
    split_file = gepa_cfg.get("split_file_path")
    if not split_file:
        return None, None
    path = _resolve_repo_path(repo_root, str(split_file))
    payload = json.loads(path.read_text(encoding="utf-8"))
    train_ids = [str(x) for x in (payload.get("train_task_ids") or [])]
    val_ids = [str(x) for x in (payload.get("val_task_ids") or [])]
    if not train_ids:
        raise ValueError(f"{path} has empty train_task_ids.")
    if not val_ids:
        raise ValueError(f"{path} has empty val_task_ids.")
    return train_ids, val_ids


def _parse_max_metric_calls(gepa: dict[str, Any]) -> int | None:
    """YAML ``max_metric_calls``: omitted → 200 (this entrypoint’s legacy default); ``null`` → no cap.

    Using ``gepa.get("max_metric_calls") or 200`` is wrong: explicit YAML ``null`` is falsy and would
    incorrectly become 200 (same bug class as :func:`domains.retail.run_gepa_optimize._parse_max_metric_calls`).
    """
    if "max_metric_calls" not in gepa:
        return 200
    raw = gepa["max_metric_calls"]
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("gepa.max_metric_calls must be an integer or null, not a boolean")
    return int(raw)


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

    parser = argparse.ArgumentParser(
        description="Tau2 retail GEPA (tau2-mermaid agent) — config-driven YAML + optional CLI overrides"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"Stacked YAML (default: {_DEFAULT_CONFIG.relative_to(repo_root) if _DEFAULT_CONFIG.is_relative_to(repo_root) else _DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Use a new timestamped directory under gepa.output_dir (also if gepa.fresh is true in YAML)",
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Resume this GEPA run directory (wins over YAML gepa.run_dir / output_dir)",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Optional experiment name. Adds a parent Logfire span and uses output dir named exactly this value.",
    )
    args = parser.parse_args()

    os.chdir(repo_root)

    cfg_path = args.config if args.config.is_absolute() else repo_root / args.config
    merged = load_stacked_yaml(repo_root, cfg_path)
    from agent.api_key_rotation import configure_from_simulation_dict
    from dotenv import load_dotenv

    load_dotenv()
    configure_from_simulation_dict(merged)
    from domains.retail.llm_seed_chain import (
        LLMSeedChain,
        LLMSeedChainCallback,
        retail_llm_seed_master_from_gepa_cfg,
    )

    gepa = merged.get("gepa") or {}
    if not gepa:
        raise ValueError(f"YAML must contain a non-empty 'gepa:' section: {cfg_path}")

    sim_raw = simulation_dict_for_solo(merged)
    evaluate_comm = domain_evaluate_communication_from_raw(sim_raw)

    domain = merged.get("domain") or {}
    instr_rel = domain.get("instructions")
    tasks_rel = domain.get("tasks")
    if not instr_rel or not tasks_rel:
        raise ValueError("Merged config must set domain.instructions and domain.tasks (via extends or inline).")
    instructions_text = _resolve_repo_path(repo_root, str(instr_rel)).read_text(encoding="utf-8")

    engine_seed = int(gepa.get("engine_seed", 0))
    opt_mode = _normalize_gepa_optimization_mode(gepa.get("optimization_mode"))

    dataset_tasks: list[dict[str, Any]] | None = None
    valset_tasks: list[dict[str, Any]] | None = None
    single_task: dict[str, Any] | None = None
    split_dataset_ids, split_valset_ids = _load_split_ids_from_file(repo_root, gepa)

    if opt_mode == "single_task":
        tid = gepa.get("task_id")
        if tid is None:
            raise ValueError("gepa.task_id is required when gepa.optimization_mode is single_task.")
        loaded = load_retail_tasks_json(repo_root, str(tasks_rel), task_ids=[str(tid)])
        if len(loaded) != 1:
            raise RuntimeError(f"Expected exactly one task for gepa.task_id={tid!r}; got {len(loaded)}.")
        single_task = loaded[0]
    elif opt_mode == "multi_task":
        d_ids = split_dataset_ids if split_dataset_ids is not None else gepa.get("dataset_task_ids")
        if not d_ids:
            raise ValueError("gepa.dataset_task_ids (non-empty list) is required when optimization_mode is multi_task.")
        ds_ids = [str(x) for x in d_ids]
        dataset_tasks = load_retail_tasks_json(repo_root, str(tasks_rel), task_ids=ds_ids)
        if not dataset_tasks:
            raise RuntimeError("No tasks loaded for gepa.dataset_task_ids; check domain.tasks.")
    else:
        d_ids = split_dataset_ids if split_dataset_ids is not None else gepa.get("dataset_task_ids")
        v_ids = split_valset_ids if split_valset_ids is not None else gepa.get("valset_task_ids")
        if not d_ids or not v_ids:
            raise ValueError(
                "gepa.dataset_task_ids and gepa.valset_task_ids (both non-empty) are required "
                "when optimization_mode is generalization."
            )
        ds_ids = [str(x) for x in d_ids]
        vs_ids = [str(x) for x in v_ids]
        overlap = set(ds_ids) & set(vs_ids)
        if overlap:
            raise ValueError(f"dataset_task_ids and valset_task_ids must be disjoint; overlap: {sorted(overlap)}")
        dataset_tasks = load_retail_tasks_json(repo_root, str(tasks_rel), task_ids=ds_ids)
        valset_tasks = load_retail_tasks_json(repo_root, str(tasks_rel), task_ids=vs_ids)
        if not dataset_tasks or not valset_tasks:
            raise RuntimeError("generalization mode: failed to load dataset or valset tasks.")

    seed_pol = gepa.get("seed_policy_path")
    if not seed_pol:
        raise ValueError("gepa.seed_policy_path is required in YAML.")
    seed_path = _resolve_repo_path(repo_root, str(seed_pol))
    if not seed_path.is_file():
        raise FileNotFoundError(f"gepa.seed_policy_path not found: {seed_path}")
    seed_candidate = seed_path.read_text(encoding="utf-8")
    seed_label = str(seed_path)

    out_base = str(gepa.get("output_dir") or "outputs/tau2_retail_mermaid")
    experiment_name = (str(args.experiment_name).strip() if args.experiment_name else None) or None
    fresh = bool(args.fresh or gepa.get("fresh"))
    if args.run_dir is not None:
        log_dir = str(Path(args.run_dir).expanduser())
        print(f"Resuming from: {log_dir}")
    elif experiment_name is not None:
        # Keep output location convention from YAML, but force leaf directory to experiment name.
        log_dir = str(Path(out_base).expanduser().parent / experiment_name)
        print(f"Experiment name override: {experiment_name} -> {log_dir}")
    elif gepa.get("run_dir"):
        log_dir = str(_resolve_repo_path(repo_root, str(gepa["run_dir"])))
        print(f"Using gepa.run_dir: {log_dir}")
    elif fresh:
        log_dir = f"{out_base.rstrip('/')}_{datetime.now().strftime('%m-%d_%H-%M-%S')}"
        print(f"Fresh run: {log_dir}")
    else:
        log_dir = out_base

    os.makedirs(log_dir, exist_ok=True)

    refl_arg = gepa.get("reflection_prompts_file")
    if refl_arg in (None, "", "null"):
        objective, background, reflection_label, optimizer, evaluator_prompt_template = (
            _resolve_reflection_prompts(None)
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
    llm_seed_chain: LLMSeedChain | None = None
    gepa_callbacks_list: list[Any] | None = None
    if use_llm_seed_chain:
        if not _gepa_cb_allowed:
            print(
                "WARNING: gepa.llm_seed_chain is true but installed GEPAConfig has no "
                "`gepa_callbacks`; seed chaining disabled. Use the repo's vendored gepa.",
                file=sys.stderr,
            )
        else:
            _master = retail_llm_seed_master_from_gepa_cfg(gepa)
            llm_seed_chain = LLMSeedChain(_master)
            gepa_callbacks_list = [LLMSeedChainCallback(llm_seed_chain)]

    reflection_lm_spec = gepa.get("reflection_lm") or "gemini/gemini-3-flash-preview"
    reflection_llm_backend = str(gepa.get("reflection_llm_backend") or "litellm").strip().lower()
    if reflection_llm_backend not in ("litellm", "genai", "openai", "anthropic"):
        raise ValueError(
            "gepa.reflection_llm_backend must be 'litellm', 'genai', 'openai', or 'anthropic' "
            f"(got {reflection_llm_backend!r})."
        )

    _dbd = gepa.get("diagnosis_llm_backend")
    if _dbd in (None, "", "null"):
        diagnosis_llm_backend = reflection_llm_backend
    else:
        diagnosis_llm_backend = str(_dbd).strip().lower()
    if diagnosis_llm_backend not in ("litellm", "genai", "openai", "anthropic"):
        raise ValueError(
            "gepa.diagnosis_llm_backend must be 'litellm', 'genai', 'openai', 'anthropic', "
            "or null (match reflection); "
            f"got {diagnosis_llm_backend!r}."
        )

    _diag = gepa.get("diagnosis_lm")
    diagnosis_lm = str(_diag).strip() if _diag is not None and str(_diag).strip() else None
    if diagnosis_lm and diagnosis_llm_backend == "genai" and diagnosis_lm.startswith("gemini/"):
        diagnosis_lm = diagnosis_lm.split("/", 1)[1].strip()
    if diagnosis_lm and diagnosis_llm_backend == "openai" and diagnosis_lm.startswith("openai/"):
        diagnosis_lm = diagnosis_lm.split("/", 1)[1].strip()
    if diagnosis_lm and diagnosis_llm_backend == "anthropic" and diagnosis_lm.startswith(
        "anthropic/"
    ):
        diagnosis_lm = diagnosis_lm.split("/", 1)[1].strip()

    _dgt = gepa.get("diagnosis_genai_temperature")
    diagnosis_genai_temperature = float(_dgt) if _dgt is not None else None
    _dgmo = gepa.get("diagnosis_genai_max_output_tokens")
    diagnosis_genai_max_output_tokens = int(_dgmo) if _dgmo is not None else None
    diagnosis_genai_reasoning_effort = (
        str(gepa.get("diagnosis_genai_reasoning_effort") or "").strip() or None
    )
    diagnosis_genai_vertex_ai = bool(gepa.get("diagnosis_genai_vertex_ai", False))

    _eval_tpl_stripped = (evaluator_prompt_template or "").strip() or None
    if _eval_tpl_stripped and diagnosis_lm:
        validate_diagnosis_prompt_template(_eval_tpl_stripped)
    elif _eval_tpl_stripped and not diagnosis_lm:
        print(
            "Note: reflection file has # Evaluator but gepa.diagnosis_lm is unset; "
            "failed-task diagnosis will not use that template.",
            file=sys.stderr,
        )
    max_metric = _parse_max_metric_calls(gepa)
    max_workers = max(1, int(gepa.get("max_workers") or 2))
    rfmt = str(gepa.get("reflective_dataset_format") or "tau2_retail").lower()
    if rfmt not in ("default", "tau2_retail"):
        raise ValueError("gepa.reflective_dataset_format must be 'default' or 'tau2_retail'")

    _ec_names = {f.name for f in dataclasses.fields(EngineConfig)}
    if rfmt == "tau2_retail" and "reflective_dataset_format" not in _ec_names:
        print(
            "Note: installed gepa has no EngineConfig.reflective_dataset_format; "
            "reflection uses the default OptimizeAnythingAdapter row format.",
            file=sys.stderr,
        )

    _rc_names = {f.name for f in dataclasses.fields(ReflectionConfig)}
    if gepa_generated_template is not None and "gepa_generated_template" not in _rc_names:
        print(
            "Note: installed gepa has no ReflectionConfig.gepa_generated_template; "
            "partial merge template is ignored.",
            file=sys.stderr,
        )

    _tc_names = {f.name for f in dataclasses.fields(TrackingConfig)}
    if bool(gepa.get("dump_visualizer_events")) and "dump_visualizer_events" not in _tc_names:
        print(
            "Note: installed gepa has no TrackingConfig.dump_visualizer_events.",
            file=sys.stderr,
        )

    mb = gepa.get("reflection_minibatch_size")
    _n_reflect = len(dataset_tasks) if dataset_tasks else 1
    if mb is None:
        minibatch = min(10, max(1, _n_reflect))
    else:
        minibatch = max(1, int(mb))

    try:
        import logfire
        from agent.logfire_gemini_integration import instrument_logfire_gemini
        from dotenv import load_dotenv

        if gepa.get("use_logfire", True):
            load_dotenv()
            from agent.telemetry import configure_logfire_tau2

            configure_logfire_tau2(
                scrubbing=False,
                console=False,
                use_gcp_trace=gepa.get("use_gcp_trace"),
            )
            instrument_logfire_gemini()
            logfire.instrument_litellm()
    except Exception as e:
        print(f"(Optional) Logfire/Gemini instrumentation skipped: {e}", file=sys.stderr)

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
                "Note: reflection_llm_backend genai uses a Python callable; enable gepa.use_cloudpickle "
                "if you need GEPA state checkpoint/resume.",
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
            print(
                "Note: reflection_llm_backend openai uses a Python callable; enable gepa.use_cloudpickle "
                "if you need GEPA state checkpoint/resume.",
                file=sys.stderr,
            )
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
            print(
                "Note: reflection_llm_backend anthropic uses a Python callable; enable gepa.use_cloudpickle "
                "if you need GEPA state checkpoint/resume.",
                file=sys.stderr,
            )
    elif reflection_llm_backend == "litellm":
        reflection_lm = str(reflection_lm_spec)
    else:
        raise AssertionError("unreachable reflection_llm_backend")

    if opt_mode == "single_task":
        assert single_task is not None

        def evaluator(candidate: str | dict[str, Any]) -> tuple[float, SideInfo]:
            return evaluate_policy_with_mermaid_agent(
                repo_root=repo_root,
                candidate=candidate,
                task=single_task,
                instructions_text=instructions_text,
                simulation_raw=sim_raw,
                evaluate_communication=evaluate_comm,
                seed=llm_seed_chain.read() if llm_seed_chain is not None else eval_seed,
                diagnosis_lm=diagnosis_lm,
                diagnosis_prompt_template=_eval_tpl_stripped,
                diagnosis_llm_backend=diagnosis_llm_backend,
                diagnosis_genai_temperature=diagnosis_genai_temperature,
                diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
            )
    else:
        assert dataset_tasks is not None

        def evaluator(candidate: str | dict[str, Any], example: dict[str, Any]) -> tuple[float, SideInfo]:
            return evaluate_policy_with_mermaid_agent(
                repo_root=repo_root,
                candidate=candidate,
                task=example,
                instructions_text=instructions_text,
                simulation_raw=sim_raw,
                evaluate_communication=evaluate_comm,
                seed=llm_seed_chain.read() if llm_seed_chain is not None else eval_seed,
                diagnosis_lm=diagnosis_lm,
                diagnosis_prompt_template=_eval_tpl_stripped,
                diagnosis_llm_backend=diagnosis_llm_backend,
                diagnosis_genai_temperature=diagnosis_genai_temperature,
                diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
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
    if _gepa_cb_allowed:
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
            val_evaluation_policy=gepa.get("val_evaluation_policy") or "full_eval",
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
    got_lf = getattr(config.tracking, "use_logfire", False)
    if want_lf and not got_lf:
        print(
            "WARNING: GEPA Logfire hierarchy (On optimization start / iteration / …) is OFF: "
            "installed TrackingConfig has no `use_logfire`. Use the repo's vendored `gepa` "
            "(see pyproject.toml [tool.uv.sources]) and run `uv sync`.",
            file=sys.stderr,
        )

    print(f"Config: {cfg_path}")
    print(f"Repo root: {repo_root}")
    print(f"evaluate_communication: {evaluate_comm}")
    if reflection_label:
        print(f"Reflection prompts file: {reflection_label}")
    else:
        print("Reflection prompts: inline defaults (utils OBJECTIVE_TRAIN_ONLY / BACKGROUND; no markdown file)")
    if optimizer_body:
        print("Reflection prompt: custom (# Optimizer template)")
    else:
        print("Reflection prompt: GEPA built-in (objective / background sections)")
    _rprint = str(reflection_lm_spec).strip()
    if reflection_llm_backend == "litellm":
        _rprint_suffix = f" (model={_rprint!r})"
    elif reflection_llm_backend == "genai":
        _rprint_suffix = f" (google.genai model id={_rprint.removeprefix('gemini/')!r})"
    elif reflection_llm_backend == "openai":
        _rprint_suffix = f" (openai model id={_rprint.removeprefix('openai/')!r})"
    elif reflection_llm_backend == "anthropic":
        _rprint_suffix = f" (anthropic model id={_rprint.removeprefix('anthropic/')!r})"
    else:
        _rprint_suffix = f" (model={_rprint!r})"
    print(f"Reflection LLM backend: {reflection_llm_backend}{_rprint_suffix}")
    if _eval_tpl_stripped and diagnosis_lm:
        print("Diagnosis prompt: custom (# Evaluator template)")
    if diagnosis_lm:
        print(
            f"Diagnosis LLM backend: {diagnosis_llm_backend} "
            f"(model={diagnosis_lm!r})"
        )
    print(
        f"GEPA optimization_mode: {opt_mode} → optimize_anything("
        f"dataset={'None' if opt_mode == 'single_task' else f'list[{len(dataset_tasks)}]'}, "
        f"valset={'None' if opt_mode != 'generalization' else f'list[{len(valset_tasks or [])}]'})"
    )
    if opt_mode == "single_task":
        print(
            f"  task_id: {single_task.get('id')!r} "
            "(evaluator: no 'example' parameter; GEPA single-instance mode)"
        )
    elif opt_mode == "multi_task":
        print(f"  dataset_task_ids: {[t.get('id') for t in dataset_tasks]}")
    else:
        print(f"  dataset_task_ids: {[t.get('id') for t in dataset_tasks]}")
        print(f"  valset_task_ids:  {[t.get('id') for t in (valset_tasks or [])]}")
    print(f"Seed policy: {seed_label}")
    print(f"Log dir: {log_dir}")
    if llm_seed_chain is not None:
        print(
            "llm_seed_chain: on "
            f"(master={retail_llm_seed_master_from_gepa_cfg(gepa)} from gepa.evaluation_seed, "
            "else legacy gepa.seed, else 42)"
        )
    elif eval_seed is not None:
        print(f"Simulator LLM seed (fixed): evaluation_seed={eval_seed}")

    try:
        import logfire as _logfire_for_span

        _cfg_name = cfg_path.name
        _top_span_label = f"tau2_retail_mermaid GEPA · {_cfg_name}"
        top_span = _logfire_for_span.span(
            _top_span_label,
            _span_name=_top_span_label,
            run_dir=log_dir,
            config_path=str(cfg_path.resolve()),
            config_file=_cfg_name,
            optimization_mode=opt_mode,
            num_dataset_tasks=len(dataset_tasks) if dataset_tasks else 0,
            num_valset_tasks=len(valset_tasks) if valset_tasks else 0,
            single_task_id=single_task.get("id") if single_task else None,
            evaluate_communication=evaluate_comm,
        )
    except Exception:
        top_span = contextlib.nullcontext()

    if opt_mode == "single_task":
        dataset_arg = None
        valset_arg = None
    elif opt_mode == "multi_task":
        dataset_arg = dataset_tasks
        valset_arg = None
    else:
        dataset_arg = dataset_tasks
        valset_arg = valset_tasks

    parent_span = (
        _logfire_for_span.span(
            experiment_name,
            _span_name=experiment_name,
            experiment_name=experiment_name,
            output_dir=log_dir,
            config_file=cfg_path.name,
        )
        if experiment_name
        else contextlib.nullcontext()
    )

    with parent_span:
        with top_span:
            if want_lf:
                try:
                    import logfire as _lf_cfg

                    _lf_cfg.info(
                        "experiment_config",
                        run_kind="gepa_tau2_retail_mermaid",
                        config_path=str(cfg_path.resolve()),
                        config_file=cfg_path.name,
                        config_json=json.dumps(merged, default=str, sort_keys=True),
                    )
                except Exception:
                    pass

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
    if opt_mode == "single_task":
        _score_label = "single-task (GEPA val metrics match the one instance)"
    elif opt_mode == "multi_task":
        _score_label = "aggregate over dataset (GEPA valset defaults to dataset)"
    else:
        _score_label = "aggregate over valset (generalization)"
    print(f"\nBest candidate — {_score_label}: {best_score:.4f}")

    out_path = Path(log_dir) / "best_policy.md"
    out_path.write_text(best or "", encoding="utf-8")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
