#!/usr/bin/env python3
"""Write outputs/manifest.json at the repo root (parent of gepa/).

Run from anywhere:
  uv run python gepa/examples/run_output_viewer/gen_outputs_manifest.py

Same logic as scripts/gen_outputs_manifest.py in layouts where that file exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    # .../gepa/examples/run_output_viewer/this_file.py -> repo root is parents[3]
    repo_root = Path(__file__).resolve().parents[3]
    out_dir = repo_root / "outputs"
    if not out_dir.is_dir():
        print(f"Missing outputs directory: {out_dir}", file=sys.stderr)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "manifest.json").write_text(json.dumps({"runs": []}, indent=2) + "\n", encoding="utf-8")
        return 0

    markers = ("best_policy.md", "candidate_tree.html", "candidates.json")
    runs: list[str] = []
    for p in sorted(out_dir.iterdir(), key=lambda x: x.name.lower(), reverse=True):
        if not p.is_dir() or p.name.startswith("."):
            continue
        if any((p / m).is_file() for m in markers):
            runs.append(p.name)

    dest = out_dir / "manifest.json"
    dest.write_text(json.dumps({"runs": runs}, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {dest} ({len(runs)} run(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
