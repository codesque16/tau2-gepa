# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors

"""Tests for incremental minibatch evaluation merge (min_errors_minibatch expansion)."""

from gepa.core.adapter import EvaluationBatch
from gepa.proposer.reflective_mutation.reflective_mutation import _merge_evaluation_batches


def test_merge_concatenates_parallel_lists() -> None:
    left = EvaluationBatch(
        outputs=[1, 2],
        scores=[0.5, 1.0],
        trajectories=["a", "b"],
        objective_scores=[{"x": 1.0}, {"x": 2.0}],
    )
    right = EvaluationBatch(
        outputs=[3],
        scores=[0.0],
        trajectories=["c"],
        objective_scores=[{"x": 0.0}],
    )
    m = _merge_evaluation_batches(left, right)
    assert m.outputs == [1, 2, 3]
    assert m.scores == [0.5, 1.0, 0.0]
    assert m.trajectories == ["a", "b", "c"]
    assert m.objective_scores == [{"x": 1.0}, {"x": 2.0}, {"x": 0.0}]


def test_merge_objective_none_padding() -> None:
    left = EvaluationBatch(outputs=[1], scores=[1.0], trajectories=["a"], objective_scores=None)
    right = EvaluationBatch(outputs=[2], scores=[0.0], trajectories=["b"], objective_scores=[{"o": 1.0}])
    m = _merge_evaluation_batches(left, right)
    assert m.objective_scores == [{}, {"o": 1.0}]


def test_merge_both_objectives_none() -> None:
    left = EvaluationBatch(outputs=[1], scores=[1.0], trajectories=None, objective_scores=None)
    right = EvaluationBatch(outputs=[2], scores=[0.5], trajectories=None, objective_scores=None)
    m = _merge_evaluation_batches(left, right)
    assert m.trajectories is None
    assert m.objective_scores is None
