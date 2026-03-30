"""Retail GEPA evaluation with **named text components** (GEPA ``dict[str, str]`` candidates)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from examples.tau2_retail_mermaid import utils as _base

from examples.tau2_retail_components.tool_code_gate import (
    apply_tool_code_gate,
    merged_tools_markdown,
)

# Base surfaces (always). ``tool_code`` is included only when ``gepa.tool_code_gate.enabled`` (see ``main.py``).
BASE_COMPONENT_KEYS: tuple[str, ...] = ("tools_markdown", "mermaid_instructions", "mermaid_graph")

# Full tuple including ``tool_code`` (tests / callers that always pass four keys).
COMPONENT_KEYS: tuple[str, ...] = (*BASE_COMPONENT_KEYS, "tool_code")


def assemble_mermaid_policy(components: dict[str, str], *, policy_prefix: str = "") -> str:
    """Build the solo policy markdown from a fixed prefix plus three optimized components.

    - ``policy_prefix`` — fixed markdown (e.g. retail agent rules) read from disk; **not** optimized by GEPA.
    - ``tools_markdown`` → temp MCP file (tool names / descriptions).
    - ``mermaid_instructions`` → **only** how to read/follow mermaid (conventions, navigation).
    - ``mermaid_graph`` → SOP global + **node policies** + ``## SOP Flowchart`` with fenced ``mermaid``.

    Assembly order: **prefix** → ``mermaid_instructions`` → ``mermaid_graph``.
    """
    prefix = (policy_prefix or "").strip()
    mi = (components.get("mermaid_instructions") or "").rstrip()
    mg = (components.get("mermaid_graph") or "").strip()
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if mi:
        parts.append(mi)
    if mg:
        parts.append(mg)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def _normalize_component_dict(
    candidate: dict[str, Any],
    keys: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Fill missing keys with empty strings for the active component set."""
    use = keys if keys is not None else COMPONENT_KEYS
    return {k: str(candidate.get(k) or "") for k in use}


def _policy_and_tools_from_candidate(
    candidate: str | dict[str, Any],
    *,
    policy_prefix: str = "",
    tools_md_override: str | None = None,
    component_keys: tuple[str, ...] | None = None,
) -> tuple[str, str]:
    if isinstance(candidate, str):
        return candidate, ""
    if not isinstance(candidate, dict):
        return str(candidate), ""
    c = _normalize_component_dict(candidate, component_keys)
    policy_text = assemble_mermaid_policy(c, policy_prefix=policy_prefix)
    tools_md = (tools_md_override if tools_md_override is not None else merged_tools_markdown(c))
    return policy_text, tools_md


def evaluate_policy_with_mermaid_components(
    *,
    repo_root: Path,
    candidate: str | dict[str, Any],
    task: dict[str, Any],
    instructions_text: str,
    simulation_raw: dict[str, Any],
    evaluate_communication: bool,
    seed: int | None,
    policy_prefix: str = "",
    diagnosis_lm: str | None = None,
    diagnosis_prompt_template: str | None = None,
    diagnosis_llm_backend: str = "litellm",
    diagnosis_genai_temperature: float | None = None,
    diagnosis_genai_max_output_tokens: int | None = None,
    diagnosis_genai_reasoning_effort: str | None = None,
    diagnosis_genai_vertex_ai: bool = False,
    tool_code_gate: dict[str, Any] | None = None,
    persisted_additional_tools_markdown: str = "",
    component_keys: tuple[str, ...] | None = None,
) -> tuple[float, dict[str, Any]]:
    """Like :func:`examples.tau2_retail_mermaid.utils.evaluate_policy_with_mermaid_agent` but for dict candidates.

    Writes merged tools markdown (``tools_markdown`` + optional fenced ``tool_code``) to a temporary
    ``.md`` file and sets ``assistant.mcp_tools_markdown_path`` so the agent and diagnosis see the
    same tool surface as the optimizer.
    """
    _base.ensure_import_paths()
    import copy

    keys = component_keys if component_keys is not None else COMPONENT_KEYS
    gate_cfg = dict(tool_code_gate or {})
    use_monty = bool(gate_cfg.get("use_monty", False))
    gate_cfg.setdefault("enabled", False)
    gate_cfg.setdefault("max_rounds", 3)
    gate_cfg.setdefault("use_monty", False)
    gate_cfg.setdefault("curator", None)
    gate_cfg.setdefault("strict_fail_score", False)

    run_tool_code_gate = "tool_code" in keys and bool(gate_cfg.get("enabled"))

    gate_meta: dict[str, Any] = {}
    if isinstance(candidate, dict):
        cand = _normalize_component_dict(candidate, keys)
        if run_tool_code_gate:
            cand2, gate_meta = apply_tool_code_gate(cand, **gate_cfg)
        else:
            cand2, gate_meta = cand, {}
        init_tc = gate_meta.get("initial_nonempty") is True
        if (
            run_tool_code_gate
            and init_tc
            and gate_meta.get("accepted") is False
            and bool(gate_cfg.get("strict_fail_score"))
        ):
            preview = assemble_mermaid_policy(cand2, policy_prefix=policy_prefix)
            err_side: dict[str, Any] = {
                "task_id": task.get("id", "?"),
                "error": "tool_code_gate_failed",
                "candidate_preview": (preview[:800] + ("..." if len(preview) > 800 else "")),
            }
            err_side["tool_code_gate"] = gate_meta
            return 0.0, err_side
        policy_text = assemble_mermaid_policy(cand2, policy_prefix=policy_prefix)
        tools_md = merged_tools_markdown(cand2, monty_label=use_monty).rstrip() + (
            persisted_additional_tools_markdown or ""
        )
    else:
        policy_text, tools_md = _policy_and_tools_from_candidate(
            candidate, policy_prefix=policy_prefix, component_keys=keys
        )
        tools_md = (tools_md or "").rstrip() + (persisted_additional_tools_markdown or "")

    preview_for_diagnosis = (
        f"{policy_text}\n\n## Tool reference (MCP markdown)\n\n{tools_md}".strip()
    )

    raw = _base._normalize_assistant_model_list(copy.deepcopy(simulation_raw))
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix="_tools.md", prefix="gepa_tools_", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tools_md)
        ab = dict(raw.get("assistant") or {})
        ab["mcp_tools_markdown_path"] = tmp_path
        raw["assistant"] = ab

        from domains.retail.run_solo_tasks import _build_simulation_config, run_one_solo_task
        from orchestrator.orchestrator import SoloStopMode

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
        tools_markdown_path = tmp_path
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
        qualitative_llm: str | None = None
        assistant_history: list[dict[str, Any]] = []

        if _base.logfire is not None:
            span_name = f"Task:{task_id}"
            with _base.logfire.span(span_name) as task_span:
                with _base.logfire.span("task_details"):
                    _base.logfire.info(
                        "task_details",
                        task_id=task_id,
                        ticket_preview=(ticket[:300] + "..." if len(ticket) > 300 else ticket),
                    )
                try:
                    with _base.logfire.span("simulation"):
                        success, eval_result = asyncio.run(_run_solo())
                except Exception as e:
                    task_span.message = f"Task:{task_id} [error]"
                    return 0.0, {
                        "task_id": task_id,
                        "error": f"{type(e).__name__}: {e}",
                        "candidate_preview": preview_for_diagnosis[:800]
                        + ("..." if len(preview_for_diagnosis) > 800 else ""),
                    }
                assistant_history = list(eval_result.pop("assistant_history", []) or [])
                with _base.logfire.span("evaluation"):
                    _base.logfire.info(
                        "DB evaluation",
                        task_id=eval_result.get("task_id", task_id),
                        db_match=eval_result.get("db_match"),
                        golden_hash=eval_result.get("golden_hash"),
                        predicted_hash=eval_result.get("predicted_hash"),
                        golden_actions_count=eval_result.get("golden_actions_count"),
                        predicted_actions_count=eval_result.get("predicted_actions_count"),
                    )
                    if evaluate_communication:
                        _base.logfire.info(
                            "Communication check",
                            task_id=eval_result.get("task_id", task_id),
                            communicate_match=eval_result.get("communicate_match"),
                            communicate_eval_skipped=eval_result.get("communicate_eval_skipped"),
                            communicate_checks=eval_result.get("communicate_checks"),
                        )
                if not success and diagnosis_lm:
                    qualitative_llm = _base._run_gepa_eval_diagnosis(
                        diagnosis_lm=diagnosis_lm,
                        diagnosis_prompt_template=diagnosis_prompt_template,
                        diagnosis_llm_backend=diagnosis_llm_backend,
                        diagnosis_genai_temperature=diagnosis_genai_temperature,
                        diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                        diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                        diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
                        mcp_command=mcp_command,
                        tools_markdown_path=tools_markdown_path,
                        ticket_text=(task.get("ticket") or "")[:8000],
                        policy_text=preview_for_diagnosis,
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
                    "candidate_preview": preview_for_diagnosis[:800]
                    + ("..." if len(preview_for_diagnosis) > 800 else ""),
                }
            assistant_history = list(eval_result.pop("assistant_history", []) or [])
            if not success and diagnosis_lm:
                qualitative_llm = _base._run_gepa_eval_diagnosis(
                    diagnosis_lm=diagnosis_lm,
                    diagnosis_prompt_template=diagnosis_prompt_template,
                    diagnosis_llm_backend=diagnosis_llm_backend,
                    diagnosis_genai_temperature=diagnosis_genai_temperature,
                    diagnosis_genai_max_output_tokens=diagnosis_genai_max_output_tokens,
                    diagnosis_genai_reasoning_effort=diagnosis_genai_reasoning_effort,
                    diagnosis_genai_vertex_ai=diagnosis_genai_vertex_ai,
                    mcp_command=mcp_command,
                    tools_markdown_path=tools_markdown_path,
                    ticket_text=(task.get("ticket") or "")[:8000],
                    policy_text=preview_for_diagnosis,
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
            "candidate_preview": preview_for_diagnosis[:800] + ("..." if len(preview_for_diagnosis) > 800 else ""),
        }
        if run_tool_code_gate and gate_meta:
            side_info["tool_code_gate"] = gate_meta
        if not success:
            if diagnosis_lm and qualitative_llm is not None:
                side_info["qualitative_asi"] = qualitative_llm
            else:
                side_info["qualitative_asi"] = _base._qualitative_asi_text(
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
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
