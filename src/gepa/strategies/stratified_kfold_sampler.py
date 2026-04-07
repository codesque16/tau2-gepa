"""Stratified train minibatch sampler aligned with rotating K-fold validation."""

from __future__ import annotations

import random
from dataclasses import dataclass

from gepa.core.adapter import DataInst
from gepa.core.data_loader import DataId, DataLoader
from gepa.core.state import GEPAState
from gepa.strategies.batch_sampler import BatchSampler


@dataclass
class StratifiedKFoldBatchSampler(BatchSampler[DataId, DataInst]):
    """Sample minibatches with explicit fail/success counts from train-only IDs.

    Excludes the current validation fold IDs from sampling on each iteration.
    """

    minibatch_size: int
    rng: random.Random
    failure_ids: set[DataId]
    success_ids: set[DataId]
    val_folds: list[list[DataId]]
    failure_quota: int | None = None

    def __post_init__(self) -> None:
        if self.minibatch_size <= 0:
            raise ValueError("minibatch_size must be >= 1")
        if not self.val_folds or any(len(f) == 0 for f in self.val_folds):
            raise ValueError("val_folds must be non-empty folds.")

    def _current_val_fold(self, state: GEPAState) -> set[DataId]:
        idx = max(0, int(state.i)) % len(self.val_folds)
        return set(self.val_folds[idx])

    def next_minibatch_ids(self, loader: DataLoader[DataId, DataInst], state: GEPAState) -> list[DataId]:
        all_ids = list(loader.all_ids())
        if not all_ids:
            raise ValueError("Cannot sample from empty loader.")
        val_ids = self._current_val_fold(state)
        train_ids = [x for x in all_ids if x not in val_ids]
        if not train_ids:
            raise ValueError("No train IDs available after excluding current validation fold.")

        fail_pool = [x for x in train_ids if x in self.failure_ids]
        succ_pool = [x for x in train_ids if x in self.success_ids]
        other_pool = [x for x in train_ids if x not in set(fail_pool) and x not in set(succ_pool)]

        self.rng.shuffle(fail_pool)
        self.rng.shuffle(succ_pool)
        self.rng.shuffle(other_pool)

        mb = min(self.minibatch_size, len(train_ids))
        if self.failure_quota is None:
            fail_target = int(round(mb * (len(fail_pool) / max(1, len(train_ids)))))
        else:
            fail_target = max(0, min(int(self.failure_quota), mb))
        succ_target = mb - fail_target

        chosen: list[DataId] = []
        chosen.extend(fail_pool[:fail_target])
        chosen.extend(succ_pool[:succ_target])

        # Backfill if one class is short.
        remaining = mb - len(chosen)
        if remaining > 0:
            leftovers = fail_pool[fail_target:] + succ_pool[succ_target:] + other_pool
            self.rng.shuffle(leftovers)
            chosen.extend(leftovers[:remaining])

        self.rng.shuffle(chosen)
        self._last_sample_provenance = {
            "sampler": "stratified_kfold",
            "current_val_fold_size": len(val_ids),
            "candidate_train_pool_size": len(train_ids),
            "requested_minibatch_size": self.minibatch_size,
            "actual_minibatch_size": len(chosen),
            "chosen_failure_count": sum(1 for x in chosen if x in self.failure_ids),
            "chosen_success_count": sum(1 for x in chosen if x in self.success_ids),
        }
        return chosen

