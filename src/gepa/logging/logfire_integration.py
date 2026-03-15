"""Logfire span callback for GEPA optimization hierarchy.

Creates nested spans around iterations, minibatches, and evaluations so that
downstream spans (e.g. gepa_eval from tau2) are properly nested and easier
to understand in Logfire. Captures candidate evolution: current_candidate_set,
pareto sets with scores, reject/accept, and tree-based parent-child lineage.
"""

from typing import Any

from gepa.core.callbacks import (
    OptimizationStartEvent,
    OptimizationEndEvent,
    CandidateAcceptedEvent,
    CandidateRejectedEvent,
    CandidateSelectedEvent,
    EvaluationEndEvent,
    EvaluationStartEvent,
    EvaluationSkippedEvent,
    IterationEndEvent,
    IterationStartEvent,
    MergeAttemptedEvent,
    MergeAcceptedEvent,
    MergeRejectedEvent,
    MinibatchSampledEvent,
    ParetoFrontUpdatedEvent,
    ProposalEndEvent,
    ProposalStartEvent,
    ReflectiveDatasetBuiltEvent,
    ValsetEvaluatedEvent,
    StateSavedEvent,
    BudgetUpdatedEvent,
    ErrorEvent,
)

class LogfireSpanCallback:
    """Creates Logfire spans for iteration and evaluation events.

    Hierarchy: gepa_optimization -> gepa evaluation (seed/val)
                                -> gepa iteration N -> candidate_Selection
                                                   -> gepa training
                                                   -> gepa optimizer completion
                                                   -> gepa_eval (from tau2)
                                                   -> evolution: candidate N (parent: K)
    candidate_Selection logs: current_candidate_set, pareto sets with scores,
    reject/accept, and tree-based evolution (parent from which prompt evolved).
    """

    def __init__(self) -> None:
        self._span_stack: list[Any] = []
        self._logfire = None
        try:
            import logfire

            self._logfire = logfire
        except ImportError:
            pass

    def _push_span(self, name: str, **attrs: Any) -> None:
        if self._logfire is None:
            return
        span = self._logfire.span(name, **attrs)
        self._span_stack.append(span)
        span.__enter__()

    def _pop_span(self) -> None:
        if self._logfire is None or not self._span_stack:
            return
        span = self._span_stack.pop()
        span.__exit__(None, None, None)

    # =========================================================================
    # Optimization Lifecycle
    # =========================================================================

    def on_optimization_start(self, event: OptimizationStartEvent) -> None:
        self._push_span("On optimization start", **event)

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        self._pop_span()  # optimization


    # =========================================================================
    # Iteration Lifecycle
    # =========================================================================

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._push_span(f"On iteration start: {event['iteration']}", **event)

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._push_span(f"On iteration end: {event['iteration']}", **event)
        self._pop_span()


    # =========================================================================
    # Candidate Selection and Sampling
    # =========================================================================

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        """Called when a candidate is selected for mutation."""
        self._push_span(f"On candidate selected: [{event['iteration']}][{event['candidate_idx']}]({event['score']:.2f})", **event)

    def on_minibatch_sampled(self, event: MinibatchSampledEvent) -> None:
        """Called when a training minibatch is sampled."""
        self._push_span(f"On minibatch sampled: [{event['iteration']}]({len(event['minibatch_ids'])}/{event['trainset_size']})", **event)

    # =========================================================================
    # Evaluation Events
    # =========================================================================

    def on_evaluation_start(self, event: EvaluationStartEvent) -> None:
        """Called before evaluating a candidate."""
        self._push_span(f"On evaluation start: [{event['iteration']}][{event['candidate_idx']}](is_seed: {event['is_seed_candidate']})", **event)

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        """Called after evaluating a candidate."""
        self._push_span(f"On evaluation end: [{event['iteration']}][{event['candidate_idx']}](is_seed: {event['is_seed_candidate']})", **event)

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        """Called when an evaluation is skipped or its results are not used."""
        self._push_span(f"On evaluation skipped: [{event['iteration']}][{event['candidate_idx']}](is_seed: {event['is_seed_candidate']})", **event)

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        """Called after a candidate is evaluated on the validation set."""
        self._push_span(f"On valset evaluated: [{event['iteration']}][{event['candidate_idx']}](is_best: {event['is_best_program']})", **event)

    # =========================================================================
    # Reflection Events
    # =========================================================================

    def on_reflective_dataset_built(self, event: ReflectiveDatasetBuiltEvent) -> None:
        """Called after building the reflective dataset."""
        self._push_span(f"On reflective dataset built: [{event['iteration']}][{event['candidate_idx']}]", **event)

    def on_proposal_start(self, event: ProposalStartEvent) -> None:
        """Called before proposing new instructions."""
        self._push_span(f"On proposal start: [{event['iteration']}](parent: {event['parent_candidate']})", **event)

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        """Called after proposing new instructions."""
        self._push_span(f"On proposal end: [{event['iteration']}]", **event)

    # =========================================================================
    # Acceptance/Rejection Events
    # =========================================================================

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        """Called when a new candidate is accepted."""
        self._push_span(f"On candidate accepted: [{event['iteration']}][new_idx: {event['new_candidate_idx']}](new_score: {event['new_score']:.2f})", **event)

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        """Called when a candidate is rejected."""
        self._push_span(f"On candidate rejected: [{event['iteration']}][old_score: {event['old_score']:.2f}](new_score: {event['new_score']:.2f})", **event)

    # =========================================================================
    # Merge Events
    # =========================================================================

    def on_merge_attempted(self, event: MergeAttemptedEvent) -> None:
        """Called when a merge is attempted."""
        self._push_span(f"On merge attempted: [{event['iteration']}]", **event)

    def on_merge_accepted(self, event: MergeAcceptedEvent) -> None:
        """Called when a merge is accepted."""
        self._push_span(f"On merge accepted: [{event['iteration']}][new_idx: {event['new_candidate_idx']}]", **event)

    def on_merge_rejected(self, event: MergeRejectedEvent) -> None:
        """Called when a merge is rejected."""
        self._push_span(f"On merge rejected: [{event['iteration']}]", **event)

    # =========================================================================
    # State Events
    # =========================================================================

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        """Called when the Pareto front is updated."""
        self._push_span(f"On pareto front updated: [{event['iteration']}])", **event)

    def on_state_saved(self, event: StateSavedEvent) -> None:
        """Called after state is saved to disk."""
        self._push_span(f"On state saved: [{event['iteration']}][{event['run_dir']}]", **event)

    # =========================================================================
    # Budget Tracking
    # =========================================================================

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        """Called when the evaluation budget is updated."""
        self._push_span(f"On budget updated: [{event['iteration']}][{event['metric_calls_used']}/{event['metric_calls_remaining']}]", **event)

    # =========================================================================
    # Error Handling
    # =========================================================================

    def on_error(self, event: ErrorEvent) -> None:
        """Called when an error occurs during optimization."""
        self._push_span(f"On error: [{event['iteration']}][{event['exception']}]", **event)

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        # Val eval span is created in the engine before _evaluate_on_valset
        self._push_span(f"On valset evaluated: [{event['iteration']}][{event['candidate_idx']}][avg score: {event['average_score']:.2f}](is_best: {event['is_best_program']})", **event)
