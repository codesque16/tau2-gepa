#!/usr/bin/env python3
"""Tau2 retail GEPA with **named text components** (GEPA ``dict[str, str]`` candidates).

Optimizes named text surfaces in parallel (round-robin or ``reflection_module_selector``), including:

- ``tools_markdown`` — tool names and descriptions (written to a temp MCP markdown path per eval).
- ``mermaid_instructions`` — **only** how to read/follow the mermaid diagram (conventions, navigation).
- ``mermaid_graph`` — SOP global + **node policies** + ``## SOP Flowchart`` (fenced mermaid); node policies and graph are optimized together.
- ``tool_code`` — optional Python design / stub code; gated (compile + optional Monty) before eval; merged into the temp tools markdown.

**Retail Agent Rules** are **not** optimized: ``gepa.fixed_retail_agent_rules_path`` is read once and prepended to every assembled policy.

Configuration: ``configs/gepa_retail_components.yaml``. Uses ``gepa.seed_components``,
``gepa.gepa_merge_templates``, and ``gepa.fixed_retail_agent_rules_path``.

Run from repo root::

  uv sync
  uv run python gepa/examples/tau2_retail_components/main.py --config configs/gepa_retail_components.yaml --fresh

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

from examples.tau2_retail_components.reflection_prompts_md import (
    build_component_reflection_templates,
    load_component_reflection_prompts_file,
    load_gepa_template_file,
    validate_diagnosis_prompt_template,
)
from examples.tau2_retail_components.utils import (
    BASE_COMPONENT_KEYS,
    assemble_mermaid_policy,
    evaluate_policy_with_mermaid_components,
)
from examples.tau2_retail_mermaid.utils import (
    domain_evaluate_communication_from_raw,
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
    _EXAMPLE_DIR / "reflection_prompts_components.md",
    _EXAMPLE_DIR.parent / "tau2_retail_mermaid" / "reflection_prompts_mermaid.md",
)
_DEFAULT_CONFIG = REPO_ROOT / "configs" / "gepa_retail_components.yaml"


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


def _load_component_reflection_bundle(
    repo_root: Path,
    path_arg: str | None,
) -> tuple[str, str, str | None, Any]:
    """Return (objective, background, label path, component reflection bundle)."""
    if path_arg in (None, "", "null"):
        for candidate in _REFLECTION_PROMPTS_FALLBACK_CHAIN:
            if candidate.is_file():
                p = candidate.resolve()
                b = load_component_reflection_prompts_file(p)
                return b.objective, b.background, str(p), b
        raise FileNotFoundError(
            "Set gepa.reflection_prompts_file or add "
            f"{_EXAMPLE_DIR / 'reflection_prompts_components.md'} (missing)."
        )

    p = Path(path_arg).expanduser().resolve()
    if not p.is_file():
        p = _resolve_repo_path(repo_root, str(path_arg))
    b = load_component_reflection_prompts_file(p)
    return b.objective, b.background, str(p), b


def main() -> None:
    repo_root = REPO_ROOT

    parser = argparse.ArgumentParser(
        description="Tau2 retail GEPA — multi-component candidates (tools + mermaid); YAML-driven."
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

    tcg_for_components = gepa.get("tool_code_gate") or {}
    tool_code_agent_enabled = bool(tcg_for_components.get("enabled", False))
    component_keys: tuple[str, ...] = (
        (*BASE_COMPONENT_KEYS, "tool_code") if tool_code_agent_enabled else BASE_COMPONENT_KEYS
    )

    seed_components_cfg = gepa.get("seed_components")
    if not seed_components_cfg or not isinstance(seed_components_cfg, dict):
        raise ValueError(
            "This entrypoint requires gepa.seed_components: a mapping of "
            f"{list(component_keys)} to markdown file paths."
        )
    seed_candidate: dict[str, str] = {}
    seed_paths: dict[str, str] = {}
    for comp in component_keys:
        rel = seed_components_cfg.get(comp)
        if not rel:
            raise ValueError(f"gepa.seed_components must define {comp!r}.")
        p = _resolve_repo_path(repo_root, str(rel))
        if not p.is_file():
            raise FileNotFoundError(f"gepa.seed_components[{comp!r}] not found: {p}")
        seed_candidate[comp] = p.read_text(encoding="utf-8")
        seed_paths[comp] = str(p)
    seed_label = json.dumps(seed_paths, sort_keys=True)

    fixed_rules_rel = gepa.get("fixed_retail_agent_rules_path")
    if not fixed_rules_rel:
        raise ValueError(
            "gepa.fixed_retail_agent_rules_path is required: markdown prepended to the policy (not a GEPA component)."
        )
    fixed_rules_path = _resolve_repo_path(repo_root, str(fixed_rules_rel))
    if not fixed_rules_path.is_file():
        raise FileNotFoundError(f"gepa.fixed_retail_agent_rules_path not found: {fixed_rules_path}")
    policy_prefix = fixed_rules_path.read_text(encoding="utf-8")

    out_base = str(gepa.get("output_dir") or "outputs/tau2_retail_components")
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
        objective, background, reflection_label, refl_bundle = _load_component_reflection_bundle(repo_root, None)
    else:
        objective, background, reflection_label, refl_bundle = _load_component_reflection_bundle(
            repo_root, str(_resolve_repo_path(repo_root, str(refl_arg)))
        )

    custom_reflection_template = build_component_reflection_templates(
        refl_bundle,
        component_names=component_keys,
    )
    reflection_for_optimize = (None, None)
    evaluator_prompt_template = refl_bundle.evaluator_prompt_template

    merge_map_cfg = gepa.get("gepa_merge_templates")
    if not merge_map_cfg or not isinstance(merge_map_cfg, dict):
        raise ValueError(
            "gepa.gepa_merge_templates is required: a mapping of each component name to a merge template "
            "file containing <gepa_generated> (see merge_templates/ in this example)."
        )
    gepa_generated_template: dict[str, str] = {}
    merge_paths: dict[str, str] = {}
    for comp in component_keys:
        rel = merge_map_cfg.get(comp)
        if not rel:
            raise ValueError(f"gepa.gepa_merge_templates must define {comp!r}.")
        tmpl_path = _resolve_repo_path(repo_root, str(rel))
        gepa_generated_template[comp] = load_gepa_template_file(str(tmpl_path))
        merge_paths[comp] = str(tmpl_path)
    gepa_template_label = json.dumps(merge_paths, sort_keys=True)

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

    def _persisted_additional_tools_markdown() -> str:
        if not tool_code_agent_enabled:
            return ""
        rel = gepa.get("tool_code_persisted_markdown_path")
        if not rel:
            return ""
        p = _resolve_repo_path(repo_root, str(rel))
        if not p.is_file():
            return ""
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        return (
            "\n\n---\n\n## Persisted additional tools (repository file)\n\n"
            + text
        )

    def _tool_code_gate_kwargs() -> dict[str, Any] | None:
        if not tool_code_agent_enabled:
            return None
        tcg = gepa.get("tool_code_gate") or {}
        base: dict[str, Any] = {
            "enabled": True,
            "max_rounds": int(tcg.get("max_rounds", 3)),
            "use_monty": bool(tcg.get("use_monty", False)),
            "curator": None,
            "strict_fail_score": bool(tcg.get("strict_fail_score", False)),
        }
        if not bool(tcg.get("curator_enabled", True)):
            return base
        try:
            from examples.tau2_retail_components.tool_code_curator import make_genai_tool_code_curator

            mid = str(
                tcg.get("curator_lm") or diagnosis_lm or reflection_lm_spec or "gemini-2.0-flash"
            ).strip()
            _cv = tcg.get("curator_genai_vertex_ai")
            curator_vertex = bool(_cv) if _cv is not None else diagnosis_genai_vertex_ai
            _ct = tcg.get("curator_temperature")
            curator_temp = float(_ct) if _ct is not None else diagnosis_genai_temperature
            _cm = tcg.get("curator_genai_max_output_tokens")
            curator_max = int(_cm) if _cm is not None else diagnosis_genai_max_output_tokens
            _cr = tcg.get("curator_genai_reasoning_effort")
            curator_effort = (
                str(_cr).strip() if _cr is not None else diagnosis_genai_reasoning_effort
            )
            base["curator"] = make_genai_tool_code_curator(
                model=mid,
                vertex_ai=curator_vertex,
                temperature=curator_temp,
                max_output_tokens=curator_max,
                reasoning_effort=curator_effort,
            )
        except Exception as e:
            print(f"WARNING: tool_code GenAI curator disabled: {e}", file=sys.stderr)
        return base

    if opt_mode == "single_task":
        assert single_task is not None

        def evaluator(candidate: str | dict[str, Any]) -> tuple[float, SideInfo]:
            return evaluate_policy_with_mermaid_components(
                repo_root=repo_root,
                candidate=candidate,
                task=single_task,
                instructions_text=instructions_text,
                simulation_raw=sim_raw,
                evaluate_communication=evaluate_comm,
                seed=llm_seed_chain.read() if llm_seed_chain is not None else eval_seed,
                policy_prefix=policy_prefix,
                diagnosis_lm=diagnosis_lm,
                diagnosis_prompt_template=_eval_tpl_stripped,
                diagnosis_llm_backend=diagnosis_llm_backend,
                diagnosis_genai_temperature=diagnosis_genai_temperature,
                diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
                tool_code_gate=_tool_code_gate_kwargs(),
                persisted_additional_tools_markdown=_persisted_additional_tools_markdown(),
                component_keys=component_keys,
            )
    else:
        assert dataset_tasks is not None

        def evaluator(candidate: str | dict[str, Any], example: dict[str, Any]) -> tuple[float, SideInfo]:
            return evaluate_policy_with_mermaid_components(
                repo_root=repo_root,
                candidate=candidate,
                task=example,
                instructions_text=instructions_text,
                simulation_raw=sim_raw,
                evaluate_communication=evaluate_comm,
                seed=llm_seed_chain.read() if llm_seed_chain is not None else eval_seed,
                policy_prefix=policy_prefix,
                diagnosis_lm=diagnosis_lm,
                diagnosis_prompt_template=_eval_tpl_stripped,
                diagnosis_llm_backend=diagnosis_llm_backend,
                diagnosis_genai_temperature=diagnosis_genai_temperature,
                diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
                tool_code_gate=_tool_code_gate_kwargs(),
                persisted_additional_tools_markdown=_persisted_additional_tools_markdown(),
                component_keys=component_keys,
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
    print(f"Reflection prompts file: {reflection_label}")
    print(f"Reflection prompt: per-component (# Optimizer / # Optimizer <name>) × {len(component_keys)}")
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
    print(f"Fixed retail rules (prefix): {fixed_rules_path}")
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
        _top_span_label = f"tau2_retail_components GEPA · {_cfg_name}"
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
                        run_kind="gepa_tau2_retail_components",
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

    out_dir = Path(log_dir) / "best_policy"
    out_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(best, dict):
        for k, v in best.items():
            (out_dir / f"{k}.md").write_text(v or "", encoding="utf-8")
        assembled = assemble_mermaid_policy(best, policy_prefix=policy_prefix)
        (out_dir / "assembled_policy.md").write_text(assembled, encoding="utf-8")
        tc_best = (
            (best.get("tool_code") or "").strip() if tool_code_agent_enabled else ""
        )
        if tc_best:
            (out_dir / "additional_tools.md").write_text(tc_best + "\n", encoding="utf-8")
        _saved_parts = "tools_markdown,mermaid_instructions,mermaid_graph"
        if tool_code_agent_enabled:
            _saved_parts += ",tool_code"
        print(
            f"Saved: {out_dir}/{{{_saved_parts},assembled_policy}}.md"
            + ("; additional_tools.md" if tc_best else "")
        )
        if (
            tool_code_agent_enabled
            and bool(gepa.get("tool_code_append_to_persisted_file", False))
            and tc_best
        ):
            rel = gepa.get("tool_code_persisted_markdown_path") or "domains/retail/additional_tools.md"
            p_append = _resolve_repo_path(repo_root, str(rel))
            p_append.parent.mkdir(parents=True, exist_ok=True)
            with p_append.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n\n## GEPA tool_code ({datetime.now().isoformat()})\n\n```python\n{tc_best}\n```\n"
                )
            print(f"Appended accepted tool_code to {p_append}")
    else:
        out_path = out_dir / "assembled_policy.md"
        out_path.write_text(best or "", encoding="utf-8")
        print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
