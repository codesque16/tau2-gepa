"""Retail GEPA evaluation using tau2-mermaid solo stack (Gemini MCP agent), not tau2-bench.

Expects to run inside the tau2-mermaid monorepo checkout (``gepa/`` is a subdirectory).
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

try:
    import logfire
except ImportError:  # pragma: no cover
    logfire = None  # type: ignore[misc, assignment]

import yaml


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base`` (dict values merge; scalars replace)."""
    out = dict(base)
    for k, v in override.items():
        if k == "extends":
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_stacked_yaml(repo_root: Path, config_path: Path, *, _depth: int = 0) -> dict[str, Any]:
    """Load YAML; if ``extends: relative/path.yaml`` is present, load parent first and deep-merge.

    Paths in ``extends`` are relative to ``repo_root`` unless absolute.
    """
    if _depth > 8:
        raise ValueError("extends chain too deep (possible cycle)")
    path = config_path if config_path.is_absolute() else repo_root / config_path
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Root of {path} must be a mapping")
    extends = raw.get("extends")
    if not extends:
        return raw
    parent_rel = Path(str(extends))
    parent = load_stacked_yaml(repo_root, parent_rel, _depth=_depth + 1)
    return deep_merge(parent, raw)


def simulation_dict_for_solo(full_config: dict[str, Any]) -> dict[str, Any]:
    """Strip GEPA-only keys so the result matches a solo simulation YAML shape."""
    skip = {"extends", "gepa"}
    return {k: v for k, v in full_config.items() if k not in skip}

# Default copy (same intent as ``examples.tau2_retail.utils``) — avoid importing tau2-bench there.
OBJECTIVE_TRAIN_ONLY = (
    "Maximize the score for each task. Score is 1.0 or 0.0 depending on whether the run was a success or not"
)

BACKGROUND = """You are optimizing the agent policy for a retail customer-service agent.

Your candidate is the current full policy document. The policy defines domain rules, action rules, and constraints.
The agent is given this policy as its domain knowledge; you are refining it for better task completion.
The agent receives a ticket with the user's request and must make the required tool calls before replying.

Common failure modes:
- Agent doesn't communicate required info to the user
- Agent gives up or times out before completing the task
- Agent makes incorrect policy assumptions
- Agent doesn't handle edge cases (e.g., partial refunds, exchange eligibility)
- Policy rules are ambiguous or missing for edge cases

Preserve the structure (markdown, sections) and improve clarity, completeness, and edge-case handling.
"""


TRAIN_ONLY_TASK_IDS = [
    "2",
    "12",
    "17",
    "23",
    "27",
    "32",
    "33",
    "34",
    "45",
    "42",
    "43",
    "56",
    "66",
    "68",
    "78",
    "73",
    "86",
    "81",
    "91",
    "113",
    "102",
    "103",
]


def tau2_mermaid_repo_roots() -> tuple[Path, Path]:
    """Return (gepa_dir, tau2_mermaid_repo_root) inferred from this file location."""
    here = Path(__file__).resolve()
    gepa_dir = here.parents[2]
    repo_root = here.parents[3]
    marker = repo_root / "domains" / "retail" / "evaluate.py"
    if not marker.is_file():
        raise RuntimeError(
            f"Cannot find tau2-mermaid repo root (missing {marker}). "
            "This example must live under tau2-mermaid/gepa/examples/tau2_retail_mermaid/."
        )
    return gepa_dir, repo_root


def ensure_import_paths() -> tuple[Path, Path]:
    """Put gepa examples parent and repo root on ``sys.path``."""
    gepa_dir, repo_root = tau2_mermaid_repo_roots()
    for p in (gepa_dir, repo_root):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
    return gepa_dir, repo_root


def load_retail_tasks_json(
    repo_root: Path,
    tasks_relpath: str,
    *,
    task_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    path = Path(tasks_relpath)
    if not path.is_absolute():
        path = repo_root / tasks_relpath
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected list of tasks in {path}")
    solo = [t for t in data if isinstance(t, dict) and t.get("solo_convertible", True)]
    if task_ids is not None and task_ids:
        allowed: set[str | int] = set(task_ids)
        allowed |= {str(x) for x in task_ids}
        allowed |= {int(x) for x in task_ids if str(x).isdigit()}
        solo = [t for t in solo if t.get("id") in allowed]
    return solo


def _reward_info_line(score: float, eval_result: dict[str, Any]) -> str:
    return (
        f"task_score={score}; db_match={eval_result.get('db_match')}; "
        f"path_match={eval_result.get('path_match')}; "
        f"golden_hash={eval_result.get('golden_hash')}; "
        f"predicted_hash={eval_result.get('predicted_hash')}"
    )


def _qualitative_asi_text(
    eval_result: dict[str, Any],
    *,
    evaluate_communication: bool,
    score: float,
) -> str:
    lines = [
        _reward_info_line(score, eval_result),
        f"golden_actions={eval_result.get('golden_actions_count')} "
        f"predicted_actions={eval_result.get('predicted_actions_count')}",
    ]
    pm = eval_result.get("path_mismatch")
    if pm:
        lines.append(f"path_mismatch: {json.dumps(pm, default=str)}")
    if evaluate_communication:
        if eval_result.get("communicate_eval_skipped"):
            lines.append("communication_check: skipped (no communicate_info in task)")
        else:
            lines.append(f"communicate_match={eval_result.get('communicate_match')}")
            for chk in eval_result.get("communicate_checks") or []:
                info = chk.get("info", "")
                met = chk.get("met")
                just = (chk.get("justification") or "")[:800]
                lines.append(f"  - [{info}] met={met}: {just}")
    return "\n".join(lines)


def _run_gepa_eval_diagnosis(
    *,
    diagnosis_lm: str,
    diagnosis_prompt_template: str | None = None,
    diagnosis_llm_backend: str = "litellm",
    diagnosis_genai_temperature: float | None = None,
    diagnosis_genai_max_output_tokens: int | None = None,
    diagnosis_genai_reasoning_effort: str | None = None,
    mcp_command: str,
    tools_markdown_path: str | None,
    ticket_text: str,
    policy_text: str,
    task: dict[str, Any],
    eval_result: dict[str, Any],
    evaluate_communication: bool,
    score: float,
    assistant_history: list[dict[str, Any]],
    task_id: Any,
    nest_gepa_eval_span: bool,
) -> str:
    """Qualitative diagnosis via ``domains.retail.gepa_qualitative`` (failed tasks only)."""
    from domains.retail.evaluate import format_solo_eval_for_gepa_diagnosis
    from domains.retail.gepa_qualitative import (
        diagnose_single_retail_failure_for_gepa,
        format_openai_style_history_dicts,
        retail_tools_list_for_gepa_diagnosis,
    )

    evaluation_text = format_solo_eval_for_gepa_diagnosis(
        task=task,
        eval_result=eval_result,
        assistant_history=assistant_history,
        score=score,
        evaluate_communication=evaluate_communication,
    )
    trace = format_openai_style_history_dicts(assistant_history) or "(empty conversation)"
    tools_list = retail_tools_list_for_gepa_diagnosis(
        mcp_command=mcp_command,
        tools_markdown_path=tools_markdown_path,
    )

    def _call() -> str:
        return diagnose_single_retail_failure_for_gepa(
            task_description=ticket_text or "(no ticket)",
            tools_list=tools_list,
            evaluation_text=evaluation_text,
            conversation_trace=trace,
            policy_preview=policy_text,
            diagnosis_lm=diagnosis_lm,
            diagnosis_prompt_template=diagnosis_prompt_template,
            diagnosis_llm_backend=diagnosis_llm_backend,
            diagnosis_genai_temperature=diagnosis_genai_temperature,
            diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
            diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
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


def _normalize_assistant_model_list(raw: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(raw)
    ab = dict(out.get("assistant") or {})
    m = ab.get("model")
    if isinstance(m, list) and m:
        ab["model"] = m[0]
    reff = ab.get("reasoning_effort")
    if isinstance(reff, list) and reff:
        ab["reasoning_effort"] = reff[0]
    out["assistant"] = ab
    return out


def evaluate_policy_with_mermaid_agent(
    *,
    repo_root: Path,
    candidate: str | dict[str, Any],
    task: dict[str, Any],
    instructions_text: str,
    simulation_raw: dict[str, Any],
    evaluate_communication: bool,
    seed: int | None,
    diagnosis_lm: str | None = None,
    diagnosis_prompt_template: str | None = None,
    diagnosis_llm_backend: str = "litellm",
    diagnosis_genai_temperature: float | None = None,
    diagnosis_genai_max_output_tokens: int | None = None,
    diagnosis_genai_reasoning_effort: str | None = None,
) -> tuple[float, dict[str, Any]]:
    """Run one retail solo task with the Gemini (or yaml-configured) agent; score = DB (+ optional comms).

    ``simulation_raw`` must match the solo YAML shape (``mode``, ``max_turns``, ``domain``, ``assistant``, …).
    """
    ensure_import_paths()
    from domains.retail.run_solo_tasks import (
        _build_simulation_config,
        run_one_solo_task,
    )
    from orchestrator.orchestrator import SoloStopMode

    policy_text = _candidate_text(candidate)
    raw = _normalize_assistant_model_list(simulation_raw)
    sim_cfg = _build_simulation_config(raw)

    domain_block = raw.get("domain") or {}
    db_path = Path(domain_block.get("db_path", "domains/retail/db.json"))
    if not db_path.is_absolute():
        db_path = repo_root / db_path

    stop_mode_str = str(domain_block.get("stop_mode", "first-text")).lower()
    stop_mode = (
        SoloStopMode.FIRST_TEXT_ONLY
        if stop_mode_str == "first-text"
        else SoloStopMode.TASK_COMPLETE_TOOL
    )

    assistant_mcps = getattr(sim_cfg.assistant, "mcps", None) or []
    tools_markdown_path = (getattr(sim_cfg.assistant, "mcp_tools_markdown_path", None) or "").strip() or None
    mcp_command = ""
    for server_cfg in assistant_mcps:
        if server_cfg.get("name") == "retail-tools" or not mcp_command:
            mcp_command = server_cfg.get("command") or server_cfg.get("commad") or ""

    task_id = task.get("id", "?")
    ticket = task.get("ticket") or ""

    async def _run_solo() -> tuple[bool, dict[str, Any]]:
        return await run_one_solo_task(
            instructions_text=instructions_text,
            policy_text=policy_text,
            task=task,
            sim_cfg=sim_cfg,
            stop_mode=stop_mode,
            db_path=db_path,
            mcp_command=mcp_command,
            seed=seed,
            quiet=True,
            include_policy=True,
            evaluate_communication=evaluate_communication,
        )

    success: bool
    eval_result: dict[str, Any]
    qualitative_llm: str | None = None  # set when task fails and diagnosis_lm is configured
    assistant_history: list[dict[str, Any]] = []

    if logfire is not None:
        span_name = f"Task:{task_id}"
        with logfire.span(span_name) as task_span:
            with logfire.span("task_details"):
                logfire.info(
                    "task_details",
                    task_id=task_id,
                    ticket_preview=(ticket[:300] + "..." if len(ticket) > 300 else ticket),
                )
            try:
                with logfire.span("simulation"):
                    success, eval_result = asyncio.run(_run_solo())
            except Exception as e:
                task_span.message = f"Task:{task_id} [error]"
                return 0.0, {
                    "task_id": task_id,
                    "error": f"{type(e).__name__}: {e}",
                    "candidate_preview": policy_text[:800] + ("..." if len(policy_text) > 800 else ""),
                }
            assistant_history = list(eval_result.pop("assistant_history", []) or [])
            with logfire.span("evaluation"):
                logfire.info(
                    "DB evaluation",
                    task_id=eval_result.get("task_id", task_id),
                    db_match=eval_result.get("db_match"),
                    golden_hash=eval_result.get("golden_hash"),
                    predicted_hash=eval_result.get("predicted_hash"),
                    golden_actions_count=eval_result.get("golden_actions_count"),
                    predicted_actions_count=eval_result.get("predicted_actions_count"),
                )
                if evaluate_communication:
                    logfire.info(
                        "Communication check",
                        task_id=eval_result.get("task_id", task_id),
                        communicate_match=eval_result.get("communicate_match"),
                        communicate_eval_skipped=eval_result.get("communicate_eval_skipped"),
                        communicate_checks=eval_result.get("communicate_checks"),
                    )
            if not success and diagnosis_lm:
                qualitative_llm = _run_gepa_eval_diagnosis(
                    diagnosis_lm=diagnosis_lm,
                    diagnosis_prompt_template=diagnosis_prompt_template,
                    diagnosis_llm_backend=diagnosis_llm_backend,
                    diagnosis_genai_temperature=diagnosis_genai_temperature,
                    diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                    diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                    mcp_command=mcp_command,
                    tools_markdown_path=tools_markdown_path,
                    ticket_text=(task.get("ticket") or "")[:8000],
                    policy_text=policy_text,
                    task=task,
                    eval_result=eval_result,
                    evaluate_communication=evaluate_communication,
                    score=0.0,
                    assistant_history=assistant_history,
                    task_id=task_id,
                    nest_gepa_eval_span=True,
                )
            outcome = "pass" if success else "fail"
            task_span.message = f"Task:{task_id} [{outcome}]"
    else:
        try:
            success, eval_result = asyncio.run(_run_solo())
        except Exception as e:
            return 0.0, {
                "task_id": task_id,
                "error": f"{type(e).__name__}: {e}",
                "candidate_preview": policy_text[:800] + ("..." if len(policy_text) > 800 else ""),
            }
        assistant_history = list(eval_result.pop("assistant_history", []) or [])
        if not success and diagnosis_lm:
            qualitative_llm = _run_gepa_eval_diagnosis(
                diagnosis_lm=diagnosis_lm,
                diagnosis_prompt_template=diagnosis_prompt_template,
                diagnosis_llm_backend=diagnosis_llm_backend,
                diagnosis_genai_temperature=diagnosis_genai_temperature,
                diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                mcp_command=mcp_command,
                tools_markdown_path=tools_markdown_path,
                ticket_text=(task.get("ticket") or "")[:8000],
                policy_text=policy_text,
                task=task,
                eval_result=eval_result,
                evaluate_communication=evaluate_communication,
                score=0.0,
                assistant_history=assistant_history,
                task_id=task_id,
                nest_gepa_eval_span=False,
            )

    score = 1.0 if success else 0.0
    ticket_text = (task.get("ticket") or "")[:8000]
    tid_key = str(task_id)

    # Reflection prompt (format_samples_tau) keeps only score, task_description, qualitative_asi.
    side_info: dict[str, Any] = {
        "score": score,
        "task_description": ticket_text or f"(task {tid_key})",
        "task_id": task_id,
        "ticket": ticket_text,
        "db_match": bool(eval_result.get("db_match", False)),
        "path_match": bool(eval_result.get("path_match", True)),
        "golden_hash": eval_result.get("golden_hash"),
        "predicted_hash": eval_result.get("predicted_hash"),
        "trace_preview": eval_result.get("trace_preview") or "",
        "path_mismatch": eval_result.get("path_mismatch"),
        "evaluate_communication": evaluate_communication,
        "communicate_match": eval_result.get("communicate_match"),
        "communicate_eval_skipped": eval_result.get("communicate_eval_skipped"),
        "communicate_checks": eval_result.get("communicate_checks"),
        "candidate_preview": policy_text[:800] + ("..." if len(policy_text) > 800 else ""),
    }
    if not success:
        if diagnosis_lm and qualitative_llm is not None:
            side_info["qualitative_asi"] = qualitative_llm
        else:
            side_info["qualitative_asi"] = _qualitative_asi_text(
                eval_result,
                evaluate_communication=evaluate_communication,
                score=score,
            )

    try:
        import gepa.optimize_anything as oa

        oa.log(f"# Task {task_id}")
        oa.log(
            f"score={score} db_match={side_info['db_match']} "
            f"communicate_match={side_info.get('communicate_match')}"
        )
        if eval_result.get("path_mismatch"):
            oa.log(f"path_mismatch={eval_result.get('path_mismatch')}")
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


def default_simulation_yaml(repo_root: Path) -> Path:
    p = repo_root / "configs" / "gemini_simulation.yaml"
    if not p.is_file():
        raise FileNotFoundError(f"Default simulation config not found: {p}")
    return p


def domain_evaluate_communication_flag(simulation_yaml: Path) -> bool:
    raw = yaml.safe_load(simulation_yaml.read_text(encoding="utf-8")) or {}
    return domain_evaluate_communication_from_raw(raw)


def domain_evaluate_communication_from_raw(raw: dict[str, Any]) -> bool:
    dom = raw.get("domain") or {}
    return bool(dom.get("evaluate_communication", False))
