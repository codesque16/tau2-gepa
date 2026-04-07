"""Fold-rotation validation policy for dynamic train/val splits."""

from __future__ import annotations

from typing import Any

from gepa.core.data_loader import DataId, DataInst, DataLoader
from gepa.core.state import GEPAState, ProgramIdx
from gepa.strategies.eval_policy import EvaluationPolicy


class KFoldRotationEvaluationPolicy(EvaluationPolicy[DataId, DataInst]):
    """Evaluate on exactly one fold per iteration (rotating by iteration index)."""

    def __init__(self, folds: list[list[DataId]]) -> None:
        if not folds or any(len(f) == 0 for f in folds):
            raise ValueError("KFoldRotationEvaluationPolicy requires non-empty folds.")
        self.folds = [list(f) for f in folds]

    def _fold_idx(self, state: GEPAState) -> int:
        return max(0, int(state.i)) % len(self.folds)

    def get_eval_batch(
        self, loader: DataLoader[DataId, DataInst], state: GEPAState, target_program_idx: ProgramIdx | None = None
    ) -> list[DataId]:
        del loader, target_program_idx
        return list(self.folds[self._fold_idx(state)])

    def get_best_program(self, state: GEPAState) -> ProgramIdx:
        best_idx, best_score, best_coverage = -1, float("-inf"), -1
        for program_idx, scores in enumerate(state.prog_candidate_val_subscores):
            coverage = len(scores)
            avg = sum(scores.values()) / coverage if coverage else float("-inf")
            if avg > best_score or (avg == best_score and coverage > best_coverage):
                best_score = avg
                best_idx = program_idx
                best_coverage = coverage
        return best_idx

    def get_valset_score(self, program_idx: ProgramIdx, state: GEPAState) -> float:
        return state.get_program_average_val_subset(program_idx)[0]

    def fold_debug_payload(self, state: GEPAState) -> dict[str, Any]:
        i = self._fold_idx(state)
        return {
            "current_fold_idx": i,
            "num_folds": len(self.folds),
            "current_fold_size": len(self.folds[i]),
        }

