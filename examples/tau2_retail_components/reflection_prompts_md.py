"""Reflection markdown for **multi-component** GEPA (tools + mermaid instructions + mermaid graph).

Extends :mod:`examples.tau2_retail_mermaid.reflection_prompts_md` with optional per-component
``# Optimizer <component_name>`` sections (level-1 heading + fenced block).

Headings:
  - ``# Objective``, ``# Background`` — required fenced blocks (same as parent).
  - ``# Optimizer`` — default reflection template for any component without its own section.
  - ``# Optimizer tools_markdown`` — template for the ``tools_markdown`` component only.
  - ``# Optimizer mermaid_instructions`` — template for ``mermaid_instructions``.
  - ``# Optimizer mermaid_graph`` — template for ``mermaid_graph``.
  - ``# Optimizer tool_code`` — template for optional Python ``tool_code`` (Monty/compile gate).
  - ``# Evaluator`` — optional (same as parent).

Each optimizer block must contain ``<curr_param>`` and ``<side_info>`` after substitution of
``<objective>`` / ``<background>`` (see :func:`build_reflection_prompt_from_optimizer_template`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt

from examples.tau2_retail_mermaid.reflection_prompts_md import (
    build_reflection_prompt_from_optimizer_template,
    load_gepa_template_file,
    validate_diagnosis_prompt_template,
    validate_gepa_generated_template,
)


@dataclass(frozen=True)
class ComponentReflectionPromptsBundle:
    objective: str
    background: str
    optimizer: str | None = None
    optimizer_components: dict[str, str] = field(default_factory=dict)
    evaluator_prompt_template: str | None = None


def _split_system_prompt_blocks(block: str, *, section: str) -> tuple[str, str]:
    """Re-use parent private helpers via duplicate logic — keep in sync with tau2_retail_mermaid."""
    import re

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
        return "", text.strip()
    user_text = text[user_m.end() :].strip()
    if not sys_m:
        return "", user_text
    system_text = text[sys_m.end() : user_m.start()].strip()
    return system_text, user_text


def _wrap_tags(system_prompt: str, user_template: str) -> str:
    from examples.tau2_retail_mermaid.reflection_prompts_md import (
        GEPA_FIRST_USER_MESSAGE_CLOSE,
        GEPA_FIRST_USER_MESSAGE_OPEN,
        GEPA_SYSTEM_PROMPT_CLOSE,
        GEPA_SYSTEM_PROMPT_OPEN,
    )

    return "\n\n".join(
        [
            f"{GEPA_SYSTEM_PROMPT_OPEN}\n{system_prompt.strip()}\n{GEPA_SYSTEM_PROMPT_CLOSE}",
            f"{GEPA_FIRST_USER_MESSAGE_OPEN}\n{user_template.strip()}\n{GEPA_FIRST_USER_MESSAGE_CLOSE}",
        ]
    )


def _normalize_h1_title(raw: str) -> str:
    return raw.strip().lstrip("#").strip().lower()


def parse_component_reflection_prompts_markdown(text: str) -> ComponentReflectionPromptsBundle:
    """Parse markdown with optional ``# Optimizer <name>`` sections."""
    md = MarkdownIt()
    tokens = md.parse(text)
    pending: str | None = None
    pending_component: str | None = None
    found: dict[str, str] = {}
    opt_components: dict[str, str] = {}
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
            if key.startswith("optimizer") and key != "optimizer":
                rest = key[len("optimizer") :].strip()
                if rest:
                    pending = "optimizer_component"
                    pending_component = rest.replace(" ", "_")
                else:
                    pending = "optimizer"
                    pending_component = None
            elif key in allowed:
                pending = key
                pending_component = None
            else:
                pending = None
                pending_component = None
            continue
        if tok.type == "fence" and pending:
            if pending == "optimizer_component":
                assert pending_component is not None
                if pending_component in opt_components:
                    raise ValueError(f"Duplicate # Optimizer section for {pending_component!r}.")
                opt_components[pending_component] = tok.content.rstrip("\n")
            elif pending in found:
                raise ValueError(f"Duplicate # {pending.title()} section or multiple fences before next heading.")
            else:
                found[pending] = tok.content.rstrip("\n")
            pending = None
            pending_component = None
        i += 1

    missing = [k for k in ("objective", "background") if k not in found]
    if missing:
        got = ", ".join(sorted(found)) or "(none)"
        raise ValueError(
            "Markdown must contain # Objective and # Background sections, each followed by a ``` fenced block. "
            f"Missing: {', '.join(missing)}; found: {got}."
        )

    for block_name in ("optimizer", "evaluator"):
        ob = found.get(block_name)
        if isinstance(ob, str) and ob.strip():
            sys_txt, user_txt = _split_system_prompt_blocks(ob, section=block_name)
            found[block_name] = _wrap_tags(sys_txt, user_txt)

    for comp_key, block in list(opt_components.items()):
        if isinstance(block, str) and block.strip():
            sys_txt, user_txt = _split_system_prompt_blocks(block, section=f"Optimizer {comp_key}")
            opt_components[comp_key] = _wrap_tags(sys_txt, user_txt)

    return ComponentReflectionPromptsBundle(
        objective=found["objective"],
        background=found["background"],
        optimizer=found.get("optimizer"),
        optimizer_components=opt_components,
        evaluator_prompt_template=found.get("evaluator"),
    )


def load_component_reflection_prompts_file(path: Path | str) -> ComponentReflectionPromptsBundle:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Reflection prompts file not found: {p}")
    return parse_component_reflection_prompts_markdown(p.read_text(encoding="utf-8"))


def build_component_reflection_templates(
    bundle: ComponentReflectionPromptsBundle,
    *,
    component_names: tuple[str, ...],
) -> dict[str, str]:
    """Build one filled reflection template per GEPA component (names must match seed keys)."""
    out: dict[str, str] = {}
    default_opt = (bundle.optimizer or "").strip()
    for name in component_names:
        raw = (bundle.optimizer_components.get(name) or default_opt).strip()
        if not raw:
            raise ValueError(
                f"Reflection prompts: add '# Optimizer {name.replace('_', ' ')}' or a default '# Optimizer' block."
            )
        out[name] = build_reflection_prompt_from_optimizer_template(
            raw,
            objective=bundle.objective,
            background=bundle.background,
        )
    return out


__all__ = [
    "ComponentReflectionPromptsBundle",
    "build_component_reflection_templates",
    "build_reflection_prompt_from_optimizer_template",
    "load_component_reflection_prompts_file",
    "load_gepa_template_file",
    "parse_component_reflection_prompts_markdown",
    "validate_diagnosis_prompt_template",
    "validate_gepa_generated_template",
]
