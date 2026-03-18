#!/usr/bin/env python3
"""Copy GEPA visualizer JSONL dumps into docs/visualizer_dump/ for static GitHub Pages (/docs).

Same workflow idea as tau2-bench's export_trajectories_for_pages.py → docs/data/.
Run from repo root or from gepa/:

  cd gepa && uv run python scripts/export_gepa_viz_for_pages.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def main() -> None:
    gepa_root = Path(__file__).resolve().parents[1]
    viz_root = gepa_root / "viz_outputs"
    out_root = gepa_root / "docs" / "visualizer_dump"

    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    runs: list[str] = []
    if viz_root.is_dir():
        for run_dir in sorted(viz_root.iterdir()):
            if not run_dir.is_dir():
                continue
            src = run_dir / "visualizer_dump"
            if not src.is_dir():
                continue
            jsonls = list(src.glob("*.jsonl"))
            if not jsonls:
                continue
            dest = out_root / run_dir.name
            dest.mkdir(parents=True)
            for j in jsonls:
                shutil.copy2(j, dest / j.name)
            runs.append(run_dir.name)

    manifest = {"runs": runs}
    (out_root / "runs.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Exported {len(runs)} run(s) to {out_root}")
    for r in runs:
        print(f"  - {r}")


if __name__ == "__main__":
    main()
