import json
import os
from dataclasses import asdict, is_dataclass
from typing import Any

from gepa.core.callbacks import (
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    GEPACallback,
    ParetoFrontUpdatedEvent,
    ValsetEvaluatedEvent,
)


def _json_friendly(obj: Any) -> Any:
    """Best-effort conversion so we can persist callback payloads."""
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_friendly(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_friendly(v) for v in obj]
    # Fallback: store string repr rather than failing the whole run.
    return str(obj)


class FilesystemDumpCallback(GEPACallback):
    """
    Dumps a subset of GEPA callback events to JSONL files for later visualization.

    Output files live under:
      <dump_dir>/visualizer_dump/
    """

    def __init__(self, dump_dir: str, run_name: str | None = None) -> None:
        self._root = dump_dir
        self._run_name = run_name or ""
        self._out_dir = os.path.join(self._root, "visualizer_dump")
        os.makedirs(self._out_dir, exist_ok=True)

    def _append(self, filename: str, record: dict[str, Any]) -> None:
        path = os.path.join(self._out_dir, filename)
        record_out = {
            "run_name": self._run_name,
            **record,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_json_friendly(record_out), ensure_ascii=False) + "\n")

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        self._append("pareto_front_updated.jsonl", dict(event=event))

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        self._append("candidate_accepted.jsonl", dict(event=event))

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        self._append("candidate_rejected.jsonl", dict(event=event))

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        # This may include per-task outputs/trajectories depending on adapter + capture_traces.
        self._append("valset_evaluated.jsonl", dict(event=event))

