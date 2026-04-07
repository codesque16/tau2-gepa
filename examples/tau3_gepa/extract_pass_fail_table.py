#!/usr/bin/env python3
"""Extract task pass/fail table from tau2/tau3 simulation artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _task_status(payload: dict[str, Any]) -> tuple[str | None, bool | None]:
    task_id = payload.get("task_id")
    if task_id is None:
        task_id = payload.get("id")
    success = payload.get("success")
    if isinstance(success, bool):
        return (str(task_id) if task_id is not None else None, success)
    ev = payload.get("evaluation")
    if isinstance(ev, dict):
        if isinstance(ev.get("db_match"), bool):
            return (str(task_id) if task_id is not None else str(ev.get("task_id")), bool(ev.get("db_match")))
        if isinstance(ev.get("success"), bool):
            return (str(task_id) if task_id is not None else str(ev.get("task_id")), bool(ev.get("success")))
    return (str(task_id) if task_id is not None else None, None)


def build_pass_fail(run_dir: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    results_json = run_dir / "results.json"
    if results_json.is_file():
        try:
            payload = json.loads(results_json.read_text(encoding="utf-8"))
            sims = payload.get("simulations") if isinstance(payload, dict) else None
            if isinstance(sims, list):
                latest_by_task: dict[str, dict[str, Any]] = {}
                for s in sims:
                    if not isinstance(s, dict):
                        continue
                    tid = s.get("task_id")
                    if tid is None:
                        continue
                    reward = None
                    ri = s.get("reward_info")
                    if isinstance(ri, dict) and isinstance(ri.get("reward"), (float, int)):
                        reward = float(ri["reward"])
                    if reward is None:
                        continue
                    ts = str(s.get("timestamp") or "")
                    key = str(tid)
                    prev = latest_by_task.get(key)
                    if prev is None or ts >= str(prev.get("timestamp") or ""):
                        latest_by_task[key] = {"timestamp": ts, "reward": reward}
                if latest_by_task:
                    return (
                        [
                            {
                                "id": tid,
                                "passed": bool(v["reward"] >= 1.0),
                                "status": "pass" if bool(v["reward"] >= 1.0) else "fail",
                                "reward": v["reward"],
                                "source_file": str(results_json),
                            }
                            for tid, v in sorted(latest_by_task.items(), key=lambda kv: (len(kv[0]), kv[0]))
                        ],
                        skipped,
                    )
        except Exception:
            skipped += 1

    for p in sorted(run_dir.rglob("*.json")):
        if p.name == "results.json":
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if not isinstance(payload, dict):
            skipped += 1
            continue
        task_id, success = _task_status(payload)
        if task_id is None or success is None:
            skipped += 1
            continue
        rows.append(
            {
                "id": task_id,
                "passed": bool(success),
                "status": "pass" if bool(success) else "fail",
                "source_file": str(p),
            }
        )
    # dedupe by latest appearance in sorted traversal
    by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_id[str(r["id"])] = r
    out = [by_id[k] for k in sorted(by_id.keys(), key=lambda x: (len(x), x))]
    return out, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract pass/fail table from simulation artifact directory.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out", default=None, type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run directory not found: {run_dir}")

    rows, skipped = build_pass_fail(run_dir)
    if not rows:
        raise RuntimeError(f"No pass/fail rows extracted from: {run_dir}")

    out_path = args.out.expanduser().resolve() if args.out else (run_dir / "task_pass_fail.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    n = len(rows)
    n_pass = sum(1 for r in rows if bool(r["passed"]))
    n_fail = n - n_pass
    print(f"Wrote: {out_path}")
    print(f"Rows: {n} (pass={n_pass}, fail={n_fail}, pass_rate={n_pass / n:.3f})")
    print(f"Skipped JSON files: {skipped}")


if __name__ == "__main__":
    main()

