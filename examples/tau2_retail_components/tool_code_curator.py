"""GenAI (``google.genai``) curator for :func:`tool_code_gate.apply_tool_code_gate` — no LiteLLM."""

from __future__ import annotations

import re
from typing import Callable

from agent.genai_gepa_lm import genai_generate_user_text


def _strip_markdown_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:python)?\s*\n([\s\S]*?)\n```\s*$", t)
    if m:
        return m.group(1).strip()
    return t


def make_genai_tool_code_curator(
    *,
    model: str,
    vertex_ai: bool = False,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> Callable[[str, str], str]:
    """Return ``(code, error) -> revised_code`` via :func:`agent.genai_gepa_lm.genai_generate_user_text`."""

    model = (model or "").strip()
    if model.startswith("gemini/"):
        model = model.split("/", 1)[1].strip()

    sys_msg = (
        "You fix Python so it passes compile() and (if applicable) a minimal sandbox run "
        "(Pydantic Monty). Return ONLY the full corrected Python source. "
        "No markdown fences, no explanation."
    )

    def curator(code: str, err: str) -> str:
        user = f"Validation error:\n{err}\n\n---\n\nCode:\n{code}\n"
        raw = genai_generate_user_text(
            model,
            user,
            system_instruction=sys_msg,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            vertex_ai=vertex_ai,
            io_phase="tool_code_curator",
            include_thoughts_when_no_level=True,
        )
        return _strip_markdown_fences((raw or "").strip())

    return curator
