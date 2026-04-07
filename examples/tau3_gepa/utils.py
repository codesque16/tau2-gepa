"""τ³-bench-fork retail simulations as GEPA evaluators (policy text candidate).

Expects the tau2-mermaid monorepo layout: ``tau3-bench-fork/`` at repo root.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import logfire
except ImportError:  # pragma: no cover
    logfire = None  # type: ignore[misc, assignment]

from examples.tau2_retail_mermaid.utils import (
    BACKGROUND,
    OBJECTIVE_TRAIN_ONLY,
    load_stacked_yaml,
)

_EXAMPLE_FILE = Path(__file__).resolve()
_GEPA_DIR = _EXAMPLE_FILE.parents[2]
REPO_ROOT = _EXAMPLE_FILE.parents[3]
TAU3_SRC = REPO_ROOT / "tau3-bench-fork" / "src"
def ensure_tau3_on_path() -> None:
    s = str(TAU3_SRC)
    if s not in sys.path:
        sys.path.insert(0, s)


def tau3_fork_roots() -> tuple[Path, Path, Path]:
    """(gepa_dir, tau2_mermaid_repo_root, tau3_src)."""
    if not (REPO_ROOT / "domains" / "retail" / "evaluate.py").is_file():
        raise RuntimeError(f"Expected tau2-mermaid repo root at {REPO_ROOT}")
    if not (TAU3_SRC / "tau2" / "gepa_runner.py").is_file():
        raise RuntimeError(f"Missing tau3-bench-fork sources: {TAU3_SRC}")
    return _GEPA_DIR, REPO_ROOT, TAU3_SRC


def load_tau3_tasks_as_dicts(
    *,
    task_ids: list[str],
    task_set_name: str | None = "retail",
    task_split_name: str | None = None,
) -> list[dict[str, Any]]:
    ensure_tau3_on_path()
    from tau2.data_model.tasks import Task
    from tau2.runner.helpers import get_tasks

    tasks = get_tasks(
        task_set_name=task_set_name or "retail",
        task_split_name=task_split_name,
        task_ids=[str(x) for x in task_ids],
        num_tasks=None,
    )
    out: list[dict[str, Any]] = []
    for t in tasks:
        if isinstance(t, Task):
            out.append(t.model_dump(mode="json"))
        else:
            out.append(dict(t))
    return out


def _task_description_from_example(example: dict[str, Any]) -> str:
    us = example.get("user_scenario")
    if isinstance(us, dict):
        ins = us.get("instructions")
        if isinstance(ins, str):
            return ins
        if isinstance(ins, dict):
            parts = [
                str(ins.get("reason_for_call") or ""),
                str(ins.get("task_instructions") or ""),
            ]
            return "\n".join(p for p in parts if p).strip()
    return str(us or example.get("id") or "")


def _run_gepa_eval_diagnosis_tau3(
    *,
    diagnosis_lm: str,
    diagnosis_prompt_template: str | None,
    diagnosis_llm_backend: str,
    diagnosis_genai_temperature: float | None,
    diagnosis_genai_max_output_tokens: int | None,
    diagnosis_genai_reasoning_effort: str | None,
    diagnosis_genai_vertex_ai: bool,
    task_description: str,
    policy_text: str,
    evaluation_text: str,
    conversation_trace: str,
    tools_list: str,
    task_id: Any,
    nest_gepa_eval_span: bool,
) -> str:
    from domains.retail.gepa_qualitative import diagnose_single_retail_failure_for_gepa

    def _call() -> str:
        return diagnose_single_retail_failure_for_gepa(
            task_description=task_description or "(no task)",
            tools_list=tools_list,
            evaluation_text=evaluation_text,
            conversation_trace=conversation_trace,
            policy_preview=policy_text,
            diagnosis_lm=diagnosis_lm,
            diagnosis_prompt_template=diagnosis_prompt_template,
            diagnosis_llm_backend=diagnosis_llm_backend,
            diagnosis_genai_temperature=diagnosis_genai_temperature,
            diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
            diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
            diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
        )

    if nest_gepa_eval_span and logfire is not None:
        with logfire.span(
            "gepa_eval",
            task_id=str(task_id),
            diagnosis_lm=diagnosis_lm,
            diagnosis_llm_backend=diagnosis_llm_backend,
        ):
            return _call()
    return _call()


def _qualitative_asi_tau3(
    *,
    db_match: bool,
    path_match: bool,
    evaluate_communication: bool,
    simulation_payload: dict[str, Any],
    score: float,
) -> str:
    lines = [
        f"task_score={score}; db_match={db_match}; path_match={path_match}",
    ]
    comm = simulation_payload.get("communicate_summary")
    if evaluate_communication and comm is not None:
        lines.append(f"communicate: {json.dumps(comm, ensure_ascii=False, default=str)[:4000]}")
    lines.append(f"reward_info_excerpt={json.dumps(simulation_payload.get('reward_info'), ensure_ascii=False, default=str)[:8000]}")
    return "\n".join(lines)


def evaluate_policy_with_tau3_simulation(
    *,
    tau3_yaml_path: Path,
    tau3_run_id: str,
    candidate: str | dict[str, Any],
    example: dict[str, Any],
    seed: int | None,
    gepa_artifact_root: Path,
    diagnosis_lm: str | None = None,
    diagnosis_prompt_template: str | None = None,
    diagnosis_llm_backend: str = "litellm",
    diagnosis_genai_temperature: float | None = None,
    diagnosis_genai_max_output_tokens: int | None = None,
    diagnosis_genai_reasoning_effort: str | None = None,
    diagnosis_genai_vertex_ai: bool = False,
    merged_overrides: dict[str, Any] | None = None,
    evaluate_communication: bool = True,
    verbose_logs: bool = False,
) -> tuple[float, dict[str, Any]]:
    """Run one τ³ retail task; return GEPA score and tau2-mermaid-shaped side_info."""
    ensure_tau3_on_path()
    from tau2.gepa_runner import (
        format_tau3_reward_for_gepa_diagnosis,
        path_mismatch_from_reward,
        retail_tools_markdown_for_gepa,
        run_gepa_evaluation_task,
        simulation_messages_to_openai_dicts,
        write_gepa_simulation_artifact,
    )
    from domains.retail.gepa_qualitative import format_openai_style_history_dicts

    policy_text = _candidate_text(candidate)
    task_id = example.get("id", "?")
    tid_key = str(task_id)
    task_description = _task_description_from_example(example)

    art_dir = gepa_artifact_root / f"task_{tid_key}"
    art_dir.mkdir(parents=True, exist_ok=True)

    tools_md = retail_tools_markdown_for_gepa()
    nest_diagnosis_span = logfire is not None

    def _run_core() -> tuple[float, dict[str, Any], bool]:
        sim = run_gepa_evaluation_task(
            yaml_config_path=tau3_yaml_path,
            run_id=tau3_run_id,
            task_id=tid_key,
            candidate_policy_text=policy_text,
            seed=seed,
            gepa_artifact_dir=art_dir,
            merged_overrides=merged_overrides,
            verbose_logs=verbose_logs,
        )
        ri = sim.reward_info
        reward = float(ri.reward) if ri is not None else 0.0
        success = reward >= 1.0
        db_match = bool(ri.db_check.db_match) if ri is not None and ri.db_check is not None else False
        path_match = True
        if ri is not None and ri.action_checks:
            path_match = all(c.action_match for c in ri.action_checks)
        pm = path_mismatch_from_reward(sim)
        comm_list = (
            [c.model_dump(mode="json") for c in ri.communicate_checks]
            if ri is not None and ri.communicate_checks
            else None
        )
        communicate_match: bool | None = None
        if ri is not None and ri.communicate_checks:
            communicate_match = all(c.met for c in ri.communicate_checks)

        action_checks_dump = (
            [c.model_dump(mode="json") for c in ri.action_checks]
            if ri is not None and ri.action_checks
            else None
        )
        nl_assertions_dump = (
            [c.model_dump(mode="json") for c in ri.nl_assertions]
            if ri is not None and ri.nl_assertions
            else None
        )
        reward_basis_dump: list[str] | None = None
        if ri is not None and ri.reward_basis:
            reward_basis_dump = [getattr(x, "value", str(x)) for x in ri.reward_basis]

        eval_dump_path = art_dir / "gepa_eval_dump.json"
        payload = {
            "reward": reward,
            "db_match": db_match,
            "path_match": path_match,
            "path_mismatch": pm,
            "reward_info": format_tau3_reward_for_gepa_diagnosis(sim, task_id=tid_key),
            "communicate_summary": comm_list,
        }
        write_gepa_simulation_artifact(
            simulation=sim,
            task_id=tid_key,
            dest_path=eval_dump_path,
        )

        openai_msgs = simulation_messages_to_openai_dicts(sim.messages)
        trace = format_openai_style_history_dicts(openai_msgs) or "(empty conversation)"

        qualitative_llm: str | None = None
        if not success and diagnosis_lm:
            qualitative_llm = _run_gepa_eval_diagnosis_tau3(
                diagnosis_lm=diagnosis_lm,
                diagnosis_prompt_template=diagnosis_prompt_template,
                diagnosis_llm_backend=diagnosis_llm_backend,
                diagnosis_genai_temperature=diagnosis_genai_temperature,
                diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
                task_description=task_description,
                policy_text=policy_text,
                evaluation_text=payload["reward_info"],
                conversation_trace=trace,
                tools_list=tools_md,
                task_id=task_id,
                nest_gepa_eval_span=nest_diagnosis_span,
            )

        score = 1.0 if success else 0.0
        side_info: dict[str, Any] = {
            "score": score,
            "task_description": task_description or f"(task {tid_key})",
            "task_id": task_id,
            "ticket": task_description,
            "db_match": db_match,
            "path_match": path_match,
            "golden_hash": None,
            "predicted_hash": None,
            "trace_preview": "",
            "path_mismatch": pm,
            "evaluate_communication": evaluate_communication,
            "communicate_match": communicate_match,
            "communicate_eval_skipped": not evaluate_communication,
            "communicate_checks": comm_list,
            "action_checks": action_checks_dump,
            "nl_assertions": nl_assertions_dump,
            "reward_basis": reward_basis_dump,
            "candidate_preview": policy_text[:800] + ("..." if len(policy_text) > 800 else ""),
            "tau3_eval_artifact": str(eval_dump_path.resolve()),
        }
        if not success:
            if diagnosis_lm and qualitative_llm is not None:
                side_info["qualitative_asi"] = qualitative_llm
            else:
                side_info["qualitative_asi"] = _qualitative_asi_tau3(
                    db_match=db_match,
                    path_match=path_match,
                    evaluate_communication=evaluate_communication,
                    simulation_payload=payload,
                    score=score,
                )
        return score, side_info, success

    if logfire is not None:
        span_name = f"Task:{task_id}"
        with logfire.span(span_name) as task_span:
            with logfire.span("task_details"):
                logfire.info(
                    "task_details",
                    task_id=task_id,
                    ticket_preview=(task_description[:300] + "..." if len(task_description) > 300 else task_description),
                )
            try:
                with logfire.span("simulation"):
                    score, side_info, ok = _run_core()
            except Exception as e:
                task_span.message = f"Task:{task_id} [error]"
                return 0.0, {
                    "task_id": task_id,
                    "error": f"{type(e).__name__}: {e}",
                    "task_description": task_description,
                    "candidate_preview": policy_text[:800] + ("..." if len(policy_text) > 800 else ""),
                }

            # Mirrors τ² evaluator phases; full spans also live under simulation → evaluation (RunTracer).
            with logfire.span("gepa_eval_summary"):
                logfire.info(
                    "DB evaluation",
                    task_id=tid_key,
                    db_match=side_info["db_match"],
                    gepa_score=side_info["score"],
                    path_match=side_info["path_match"],
                )
                logfire.info(
                    "Action check",
                    task_id=tid_key,
                    path_match=side_info["path_match"],
                    action_checks=side_info.get("action_checks"),
                )
                if evaluate_communication:
                    logfire.info(
                        "Communication check",
                        task_id=tid_key,
                        communicate_match=side_info.get("communicate_match"),
                    )
                basis = side_info.get("reward_basis") or []
                if "NL_ASSERTION" in basis:
                    with logfire.span("NL assertions check"):
                        logfire.info(
                            "NL assertions (task basis includes NL_ASSERTION)",
                            task_id=tid_key,
                            nl_assertions=side_info.get("nl_assertions"),
                        )

            outcome = "pass" if ok else "fail"
            task_span.message = f"Task:{task_id} [{outcome}]"
    else:
        try:
            score, side_info, _success = _run_core()
        except Exception as e:
            return 0.0, {
                "task_id": task_id,
                "error": f"{type(e).__name__}: {e}",
                "task_description": task_description,
                "candidate_preview": policy_text[:800] + ("..." if len(policy_text) > 800 else ""),
            }

    try:
        import gepa.optimize_anything as oa

        oa.log(f"# Task {task_id}")
        oa.log(f"score={score} db_match={side_info['db_match']}")
        if side_info.get("path_mismatch"):
            oa.log(f"path_mismatch={side_info.get('path_mismatch')}")
    except Exception:
        pass

    return score, side_info


def _candidate_text(candidate: str | dict[str, Any]) -> str:
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, dict):
        v = (
            candidate.get("current_candidate")
            or candidate.get("__str_candidate__")
            or next((x for x in candidate.values() if isinstance(x, str)), None)
        )
        return v if isinstance(v, str) else str(candidate)
    return str(candidate)


def merge_tau3_yaml_defaults(repo_root: Path, stacked_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load GEPA stacked YAML; return ``(merged_root, tau3_block)``."""
    merged = load_stacked_yaml(repo_root, stacked_path)
    tau3 = merged.get("tau3") or {}
    return merged, tau3 if isinstance(tau3, dict) else {}
