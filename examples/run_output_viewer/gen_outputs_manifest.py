#!/usr/bin/env python3
"""Write manifest.json for GEPA runs under viz_outputs/ only (never outputs/).

Writes to the same directory that holds the runs:
  - viz_outputs/manifest.json if runs live in repo-root viz_outputs/
  - gepa/viz_outputs/manifest.json if runs live there (typical monorepo layout)

Prefers repo-root viz_outputs/ when it contains any run; otherwise uses gepa/viz_outputs/.

Run from repo root (standalone fork):
  uv run python examples/run_output_viewer/gen_outputs_manifest.py

Monorepo (examples under gepa/):
  uv run python gepa/examples/run_output_viewer/gen_outputs_manifest.py
"""

from __future__ import annotations

import json
from pathlib import Path

MARKERS = ("best_policy.md", "candidate_tree.html", "candidates.json")


def _repo_root() -> Path:
    here = Path(__file__).resolve().parent
    examples_dir = here.parent
    parent_of_examples = examples_dir.parent
    if parent_of_examples.name == "gepa":
        return parent_of_examples.parent
    return parent_of_examples


def _dir_has_run_subdirs(d: Path) -> bool:
    if not d.is_dir():
        return False
    for p in d.iterdir():
        if not p.is_dir() or p.name.startswith("."):
            continue
        if any((p / m).is_file() for m in MARKERS):
            return True
    return False


def _resolve_viz_tree(repo: Path) -> tuple[Path, str]:
    """Return (directory, basePath for URLs)."""
    root_viz = repo / "viz_outputs"
    gepa_viz = repo / "gepa" / "viz_outputs"
    if _dir_has_run_subdirs(root_viz):
        return root_viz, "viz_outputs"
    if _dir_has_run_subdirs(gepa_viz):
        return gepa_viz, "gepa/viz_outputs"
    return root_viz, "viz_outputs"


def _collect_entries(viz_dir: Path, base_path: str) -> list[dict[str, str]]:
    if not viz_dir.is_dir():
        return []
    entries: list[dict[str, str]] = []
    for p in sorted(viz_dir.iterdir(), key=lambda x: x.name.lower(), reverse=True):
        if not p.is_dir() or p.name.startswith("."):
            continue
        if not any((p / m).is_file() for m in MARKERS):
            continue
        entries.append({"basePath": base_path, "name": p.name})
    return entries


def main() -> int:
    repo_root = _repo_root()
    viz_dir, base_path = _resolve_viz_tree(repo_root)
    entries = _collect_entries(viz_dir, base_path)

    manifest: dict = {"version": 1, "entries": entries}
    text = json.dumps(manifest, indent=2) + "\n"

    viz_dir.mkdir(parents=True, exist_ok=True)
    dest = viz_dir / "manifest.json"
    dest.write_text(text, encoding="utf-8")
    print(f"Wrote {dest} ({len(entries)} run(s) from {base_path}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
