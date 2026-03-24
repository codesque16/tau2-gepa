"""Load GEPA objective, background, and optional templates from structured markdown.

Expected shape (level-1 headings; body is the first fenced code block after each):

    # Objective

    ```
    ...
    ```

    # Background

    ```
    ...
    ```

Optional:

    # Optimizer

    ```
    Full reflection prompt template for GEPA. Must include literal ``<curr_param>`` (current policy) and
    ``<side_info>`` (evaluation feedback); GEPA fills those at runtime. Use ``<objective>`` and
    ``<background>`` where the # Objective / # Background text should be inlined (same file).

    Pass the result through :func:`build_reflection_prompt_from_optimizer_template` (done in ``main.py``).
    ```

Optional:

    # Evaluator

    ```
    Diagnostic/evaluator prompt template used by the qualitative diagnosis LLM (solo / mermaid stack).
    It must be compatible with Python ``string.Template`` substitution and must include exactly
    these placeholders (all required):
      - $task_desc
      - $tools_list
      - $reward_info
      - $trace
      - $policy_preview
    Any missing/unknown placeholder is an error.
    ```

Headings are matched case-insensitively; optional spaces after ``#`` are allowed.
Parsed using ``markdown-it-py`` (token stream).

GEPA merge templates (``<gepa_generated>`` …) are loaded from a separate file; see ``main.py``
``gepa.gepa_template_file``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

# ``string.Template`` placeholders for ``# Evaluator`` (qualitative diagnosis).
DIAGNOSIS_PROMPT_PLACEHOLDERS = frozenset(
    {"$task_desc", "$tools_list", "$reward_info", "$trace", "$policy_preview"}
)

GEPA_GENERATED_OPEN = "<gepa_generated>"

# Markers embedded in the optimizer / evaluator prompt templates so our GEPA
# LM wrappers can build proper "system" + "first user message" requests.
GEPA_SYSTEM_PROMPT_OPEN = "<gepa_system_prompt>"
GEPA_SYSTEM_PROMPT_CLOSE = "</gepa_system_prompt>"
GEPA_FIRST_USER_MESSAGE_OPEN = "<gepa_first_user_message>"
GEPA_FIRST_USER_MESSAGE_CLOSE = "</gepa_first_user_message>"


def _split_system_prompt_and_first_user_message(block: str, *, section: str) -> tuple[str, str]:
    """
    Split a fenced-block template into:
      - system prompt text (from "### System Prompt" subsection)
      - first user message template text (from "### First User Message Template" subsection)

    If the expected subsections are not found, we fall back to the entire block
    being treated as the first user message template and the system prompt as empty.
    """
    text = block or ""
    if not text.strip():
        return "", ""

    sys_re = re.compile(r"^###\s*System\s*Prompt\s*$", flags=re.IGNORECASE | re.MULTILINE)
    user_re = re.compile(
        r"^###\s*First\s*User\s*Message\s*(Template)?\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )

    sys_m = sys_re.search(text)
    user_m = user_re.search(text)
    if not user_m:
        # Backward compatible: treat whole block as user message.
        return "", text.strip()

    user_text = text[user_m.end() :].strip()
    if not sys_m:
        return "", user_text

    system_text = text[sys_m.end() : user_m.start()].strip()
    return system_text, user_text


def _wrap_system_and_first_user_message(system_prompt: str, user_template: str) -> str:
    system_prompt = system_prompt or ""
    user_template = user_template or ""
    return "\n\n".join(
        [
            f"{GEPA_SYSTEM_PROMPT_OPEN}\n{system_prompt.strip()}\n{GEPA_SYSTEM_PROMPT_CLOSE}",
            f"{GEPA_FIRST_USER_MESSAGE_OPEN}\n{user_template.strip()}\n{GEPA_FIRST_USER_MESSAGE_CLOSE}",
        ]
    )


@dataclass(frozen=True)
class ReflectionPromptsBundle:
    objective: str
    background: str
    optimizer: str | None = None
    evaluator_prompt_template: str | None = None


def _normalize_h1_title(raw: str) -> str:
    return raw.strip().lstrip("#").strip().lower()


def build_reflection_prompt_from_optimizer_template(
    template: str,
    *,
    objective: str,
    background: str,
) -> str:
    """Substitute ``<objective>`` and ``<background>``; keep ``<curr_param>`` / ``<side_info>`` for runtime.

    Raises:
        ValueError: If the result is missing required GEPA placeholders.
    """
    filled = template.replace("<objective>", objective).replace("<background>", background)
    from gepa.strategies.instruction_proposal import InstructionProposalSignature

    InstructionProposalSignature.validate_prompt_template(filled)
    return filled


def validate_gepa_generated_template(template: str) -> None:
    """Ensure template can be used with GEPA's reflective merge (see ``_apply_gepa_generated_template``)."""
    if GEPA_GENERATED_OPEN not in template:
        raise ValueError(
            "GEPA template must contain '<gepa_generated>' so reflection output can be merged. "
            "Use block form '<gepa_generated>\\n...</gepa_generated>' or a lone '<gepa_generated>' placeholder."
        )


def load_gepa_template_file(path: Path | str) -> str:
    """Read a plain-text merge template from disk and validate."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"GEPA template file not found: {p}")
    text = p.read_text(encoding="utf-8")
    validate_gepa_generated_template(text)
    return text


def parse_reflection_prompts_markdown(text: str) -> ReflectionPromptsBundle:
    """Parse markdown: required # Objective and # Background; optional # Optimizer and # Evaluator."""
    md = MarkdownIt()
    tokens = md.parse(text)
    pending: str | None = None
    found: dict[str, str] = {}
    i = 0
    allowed = frozenset(("objective", "background", "optimizer", "evaluator"))
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open" and tok.tag == "h1":
            title = ""
            i += 1
            if i < len(tokens) and tokens[i].type == "inline":
                title = tokens[i].content
                i += 1
            if i < len(tokens) and tokens[i].type == "heading_close":
                i += 1
            key = _normalize_h1_title(title)
            pending = key if key in allowed else None
            continue
        if tok.type == "fence" and pending:
            if pending in found:
                raise ValueError(f"Duplicate # {pending.title()} section or multiple fences before next heading.")
            found[pending] = tok.content.rstrip("\n")
            pending = None
        i += 1

    missing = [k for k in ("objective", "background") if k not in found]
    if missing:
        got = ", ".join(sorted(found)) or "(none)"
        raise ValueError(
            "Markdown must contain # Objective and # Background sections, each followed by a ``` fenced block. "
            f"Missing: {', '.join(missing)}; found: {got}."
        )

    # Convert the # Optimizer / # Evaluator fenced blocks into a single template string
    # that embeds system + first user-message parts with explicit tags.
    optimizer_block = found.get("optimizer")
    if isinstance(optimizer_block, str) and optimizer_block.strip():
        sys_txt, user_txt = _split_system_prompt_and_first_user_message(
            optimizer_block, section="Optimizer"
        )
        found["optimizer"] = _wrap_system_and_first_user_message(sys_txt, user_txt)

    evaluator_block = found.get("evaluator")
    if isinstance(evaluator_block, str) and evaluator_block.strip():
        sys_txt, user_txt = _split_system_prompt_and_first_user_message(
            evaluator_block, section="Evaluator"
        )
        found["evaluator"] = _wrap_system_and_first_user_message(sys_txt, user_txt)

    return ReflectionPromptsBundle(
        objective=found["objective"],
        background=found["background"],
        optimizer=found.get("optimizer"),
        evaluator_prompt_template=found.get("evaluator"),
    )


def load_reflection_prompts_file(path: Path | str) -> ReflectionPromptsBundle:
    """Read reflection prompts markdown from disk."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Reflection prompts file not found: {p}")
    return parse_reflection_prompts_markdown(p.read_text(encoding="utf-8"))


def validate_diagnosis_prompt_template(template: str) -> None:
    """Ensure ``# Evaluator`` body is compatible with :class:`string.Template`.

    Raises:
        ValueError: If required placeholders are missing or unknown ``$name`` tokens appear.
    """
    placeholder_tokens = set(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", template))
    missing = DIAGNOSIS_PROMPT_PLACEHOLDERS - placeholder_tokens
    if missing:
        raise ValueError(
            "Diagnosis / # Evaluator template is missing required placeholders: "
            + ", ".join(sorted(missing))
        )
    unknown = placeholder_tokens - DIAGNOSIS_PROMPT_PLACEHOLDERS
    if unknown:
        raise ValueError(
            "Diagnosis template contains unknown placeholders: "
            + ", ".join(sorted(unknown))
            + ". Allowed: "
            + ", ".join(sorted(DIAGNOSIS_PROMPT_PLACEHOLDERS))
        )


def load_objective_background_file(path: Path | str) -> tuple[str, str]:
    """Load objective and background from a reflection prompts markdown file."""
    b = load_reflection_prompts_file(path)
    return b.objective, b.background


def parse_objective_background_markdown(text: str) -> tuple[str, str]:
    """Parse markdown; returns (objective, background)."""
    b = parse_reflection_prompts_markdown(text)
    return b.objective, b.background
