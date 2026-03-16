# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

import random

from gepa.core.state import GEPAState
from gepa.gepa_utils import (
    get_pareto_selection_distribution,
    idxmax,
    select_program_candidate_from_pareto_front,
)
from gepa.proposer.reflective_mutation.base import CandidateSelector


class ParetoCandidateSelector(CandidateSelector):
    def __init__(self, rng: random.Random | None):
        if rng is None:
            self.rng = random.Random(0)
        else:
            self.rng = rng

    def select_candidate_idx(self, state: GEPAState) -> int:
        assert len(state.program_full_scores_val_set) == len(state.program_candidates)
        return select_program_candidate_from_pareto_front(
            state.get_pareto_front_mapping(),
            state.per_program_tracked_scores,
            self.rng,
        )

    def get_selection_distribution(self, state: GEPAState) -> dict[int, float]:
        """Probability mass over candidate indices (for logging)."""
        return get_pareto_selection_distribution(
            state.get_pareto_front_mapping(),
            state.per_program_tracked_scores,
        )


class CurrentBestCandidateSelector(CandidateSelector):
    def __init__(self):
        pass

    def select_candidate_idx(self, state: GEPAState) -> int:
        assert len(state.program_full_scores_val_set) == len(state.program_candidates)
        return idxmax(state.program_full_scores_val_set)

    def get_selection_distribution(self, state: GEPAState) -> dict[int, float]:
        """Probability mass over candidate indices (for logging)."""
        best = idxmax(state.program_full_scores_val_set)
        return {best: 1.0}


class EpsilonGreedyCandidateSelector(CandidateSelector):
    def __init__(self, epsilon: float, rng: random.Random | None):
        assert 0.0 <= epsilon <= 1.0
        self.epsilon = epsilon
        if rng is None:
            self.rng = random.Random(0)
        else:
            self.rng = rng

    def select_candidate_idx(self, state: GEPAState) -> int:
        assert len(state.program_full_scores_val_set) == len(state.program_candidates)
        if self.rng.random() < self.epsilon:
            return self.rng.randint(0, len(state.program_candidates) - 1)
        else:
            return idxmax(state.program_full_scores_val_set)

    def get_selection_distribution(self, state: GEPAState) -> dict[int, float]:
        """Probability mass over candidate indices (for logging)."""
        n = len(state.program_candidates)
        best = idxmax(state.program_full_scores_val_set)
        # P(best) = (1 - epsilon) + epsilon/n; P(other) = epsilon/n
        return {
            i: (1.0 - self.epsilon) + self.epsilon / n if i == best else self.epsilon / n
            for i in range(n)
        }


class TopKParetoCandidateSelector(CandidateSelector):
    """Pareto selection restricted to the top K programs by aggregate score."""

    def __init__(self, k: int, rng: random.Random | None):
        assert k > 0
        self.k = k
        if rng is None:
            self.rng = random.Random(0)
        else:
            self.rng = rng

    def select_candidate_idx(self, state: GEPAState) -> int:
        assert len(state.program_full_scores_val_set) == len(state.program_candidates)
        scores = state.per_program_tracked_scores
        top_k_indices = set(sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: self.k])
        pareto_mapping = state.get_pareto_front_mapping()
        filtered_mapping = {
            key: prog_set & top_k_indices
            for key, prog_set in pareto_mapping.items()
            if prog_set & top_k_indices
        }
        if not filtered_mapping:
            return idxmax(scores)
        return select_program_candidate_from_pareto_front(filtered_mapping, scores, self.rng)

    def get_selection_distribution(self, state: GEPAState) -> dict[int, float]:
        """Probability mass over candidate indices (for logging)."""
        scores = state.per_program_tracked_scores
        top_k_indices = set(sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: self.k])
        pareto_mapping = state.get_pareto_front_mapping()
        filtered_mapping = {
            key: prog_set & top_k_indices
            for key, prog_set in pareto_mapping.items()
            if prog_set & top_k_indices
        }
        if not filtered_mapping:
            best = idxmax(scores)
            return {best: 1.0}
        return get_pareto_selection_distribution(filtered_mapping, scores)
