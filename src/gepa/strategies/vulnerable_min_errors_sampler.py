# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""Minibatch sampling with optional minimum failure count and a persistent vulnerable set."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from gepa.core.adapter import DataInst
from gepa.core.data_loader import DataId, DataLoader
from gepa.core.state import GEPAState


@dataclass
class VulnerableMinErrorsBatchSampler:
    """Sample training minibatches with optional vulnerable-set bias and min-error expansion.

    **Composition (each iteration)**

    1. Take up to ``vulnerable_minibatch_size`` examples sampled uniformly at random **without
       replacement** from the *vulnerable set* (``error_ids | corrected_ids``), capped by
       ``reflection_minibatch_size``.
    2. Fill the remainder up to ``reflection_minibatch_size`` with random training ids
       (disjoint), without replacement.

    **Min-error expansion** (handled by :class:`~gepa.proposer.reflective_mutation.ReflectiveMutationProposer`)

    If ``min_errors_minibatch`` is set, after the initial evaluate on that batch, more random
    training ids are appended (never duplicating) until at least that many examples score below
    ``perfect_score``, or the training pool is exhausted. Each appended id is evaluated once on the
    parent program; scores are merged with earlier chunks in the same expansion (no duplicate work
    for ids already evaluated that iteration). Metric budget counts each example run once.

    **Vulnerable set updates** (via :meth:`record_training_eval`)

    After each reflection attempt, compare ``scores_before`` (parent) vs ``scores_after`` (proposal)
    on the same ids. Examples that fail the parent go to ``error_ids``; examples that improve from
    fail→pass go to ``corrected_ids``. On aggregate **rejection** (engine), parent failures are
    reinforced in ``error_ids``.

    Parameters
    ----------
    minibatch_size:
        Target base size before expansion (same role as ``reflection_minibatch_size``).
    rng:
        Random source (shared with GEPA engine for reproducibility).
    perfect_score:
        Scores strictly below this count as failures / errors.
    min_errors_minibatch:
        If set, expand the batch until at least this many errors appear or train is exhausted.
        If ``None``, no expansion beyond the composed minibatch.
    vulnerable_minibatch_size:
        If set, always try to include up to this many ids from the vulnerable set (subject to
        availability and ``minibatch_size``). If ``None``, composition is purely random from train
        (same as filling step only).
    """

    minibatch_size: int
    rng: random.Random
    perfect_score: float | None = None
    min_errors_minibatch: int | None = None
    vulnerable_minibatch_size: int | None = None
    error_ids: set[DataId] = field(default_factory=set)
    corrected_ids: set[DataId] = field(default_factory=set)

    def _perfect_threshold(self) -> float:
        return 1.0 if self.perfect_score is None else float(self.perfect_score)

    def vulnerable_union(self) -> set[DataId]:
        return set(self.error_ids) | set(self.corrected_ids)

    def count_errors(self, scores: list[float | None]) -> int:
        t = self._perfect_threshold()
        return sum(1 for s in scores if s is not None and s < t)

    def next_minibatch_ids(self, loader: DataLoader[DataId, DataInst], state: GEPAState) -> list[DataId]:
        del state  # iteration-independent for now; reserved for future curricula
        all_ids = list(loader.all_ids())
        n_train = len(all_ids)
        if n_train == 0:
            raise ValueError("Cannot sample a minibatch from an empty loader.")

        mb = min(self.minibatch_size, n_train)
        chosen: list[DataId] = []
        chosen_set: set[DataId] = set()

        vuln = [x for x in self.vulnerable_union() if x in set(all_ids)]
        self.rng.shuffle(vuln)

        v_cap = self.vulnerable_minibatch_size or 0
        if v_cap > 0 and vuln:
            take_v = min(v_cap, len(vuln), mb)
            for x in vuln[:take_v]:
                chosen.append(x)
                chosen_set.add(x)

        pool = [x for x in all_ids if x not in chosen_set]
        self.rng.shuffle(pool)
        n_from_vulnerable = len(chosen)
        while len(chosen) < mb and pool:
            x = pool.pop()
            chosen.append(x)
            chosen_set.add(x)

        self._last_sample_provenance = {
            "sampler": "vulnerable_min_errors",
            "target_minibatch_size": mb,
            "vulnerable_ids": list(chosen[:n_from_vulnerable]),
            "random_train_fill_ids": list(chosen[n_from_vulnerable:]),
        }
        return chosen

    def record_training_eval(
        self,
        ids: list[DataId],
        scores_before: list[float | None],
        scores_after: list[float | None],
        *,
        accepted: bool | None,
    ) -> None:
        """Update error/corrected sets from a reflection minibatch before/after scores."""
        t = self._perfect_threshold()
        for eid, sb, sa in zip(ids, scores_before, scores_after, strict=True):
            sb_f = sb is not None and sb < t
            sa_f = sa is not None and sa < t
            if sb_f:
                self.error_ids.add(eid)
            if sb_f and not sa_f:
                self.corrected_ids.add(eid)
            if sa_f:
                self.error_ids.add(eid)

        if accepted is False:
            for eid, sb in zip(ids, scores_before, strict=True):
                if sb is not None and sb < t:
                    self.error_ids.add(eid)
