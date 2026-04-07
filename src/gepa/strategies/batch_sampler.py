# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

import random
from collections import Counter
from typing import Protocol

from gepa.core.adapter import DataInst
from gepa.core.data_loader import DataId, DataLoader
from gepa.core.state import GEPAState


class BatchSampler(Protocol[DataId, DataInst]):
    def next_minibatch_ids(self, loader: DataLoader[DataId, DataInst], state: GEPAState) -> list[DataId]: ...


class EpochShuffledBatchSampler(BatchSampler[DataId, DataInst]):
    """
    Mirrors the original batching logic:
    - Shuffle ids each epoch
    - Pad so the id list length is a multiple of the **effective** minibatch (see below)
    - Deterministic via ``rng``

    When ``len(trainset) < minibatch_size``, the effective minibatch is ``len(trainset)`` (no
    duplicate padding to fill ``minibatch_size``). Padding duplicates only happen when
    ``len(trainset) > minibatch_size`` to complete a final partial chunk.
    """

    def __init__(self, minibatch_size: int, rng: random.Random | None = None):
        self.minibatch_size = minibatch_size
        self.shuffled_ids: list[DataId] = []
        self.epoch = -1
        self.id_freqs = Counter()
        self.last_trainset_size = 0
        self._effective_mb: int = minibatch_size
        if rng is None:
            self.rng = random.Random(0)
        else:
            self.rng = rng

    @staticmethod
    def _effective_minibatch(minibatch_size: int, trainset_size: int) -> int:
        """At most one full pass over the train set per step when it is smaller than ``minibatch_size``."""
        if trainset_size <= 0:
            return minibatch_size
        return min(minibatch_size, trainset_size)

    def _update_shuffled(self, loader: DataLoader[DataId, DataInst]) -> None:
        all_ids = list(loader.all_ids())
        trainset_size = len(loader)
        self.last_trainset_size = trainset_size

        if trainset_size == 0:
            self.shuffled_ids = []
            self.id_freqs = Counter()
            return

        mb = self._effective_minibatch(self.minibatch_size, trainset_size)
        self._effective_mb = mb

        self.shuffled_ids = list(all_ids)
        self.rng.shuffle(self.shuffled_ids)
        self.id_freqs = Counter(self.shuffled_ids)

        mod = trainset_size % mb
        num_to_pad = (mb - mod) if mod != 0 else 0
        if num_to_pad > 0:
            for _ in range(num_to_pad):
                selected_id = self.id_freqs.most_common()[::-1][0][0]
                self.shuffled_ids.append(selected_id)
                self.id_freqs[selected_id] += 1

    def next_minibatch_ids(self, loader: DataLoader[DataId, DataInst], state: GEPAState) -> list[DataId]:
        trainset_size = len(loader)
        if trainset_size == 0:
            raise ValueError("Cannot sample a minibatch from an empty loader.")

        mb = self._effective_minibatch(self.minibatch_size, trainset_size)
        base_idx = state.i * mb
        curr_epoch = 0 if self.epoch == -1 else base_idx // max(len(self.shuffled_ids), 1)

        needs_refresh = not self.shuffled_ids or trainset_size != self.last_trainset_size or curr_epoch > self.epoch
        if needs_refresh:
            self.epoch = curr_epoch
            self._update_shuffled(loader)
            mb = self._effective_mb

        assert len(self.shuffled_ids) >= mb
        assert len(self.shuffled_ids) % mb == 0

        base_idx = base_idx % len(self.shuffled_ids)
        end_idx = base_idx + mb
        assert end_idx <= len(self.shuffled_ids)
        out = self.shuffled_ids[base_idx:end_idx]
        self._last_sample_provenance = {
            "sampler": "epoch_shuffled",
            "effective_minibatch_size": mb,
            "epoch_index": self.epoch,
            "slice": [base_idx, end_idx],
        }
        return out
