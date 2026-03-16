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
    StateSavedEvent,
    TrainingEndEvent,
    TrainingStartEvent,
    ValsetEvaluatedEvent,
    ValsetEvaluationStartEvent,
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
        run_dir = (event.get("config") or {}).get("run_dir") or ""
        name = f"On optimization start ({run_dir})" if run_dir else "On optimization start"
        self._push_span(name, **event)

    def on_optimization_end(self, event: OptimizationEndEvent) -> None:
        self._pop_span()  # optimization

    # =========================================================================
    # Iteration Lifecycle
    # =========================================================================

    def on_iteration_start(self, event: IterationStartEvent) -> None:
        self._push_span(f"On iteration start: {event['iteration']}", **event)

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._pop_span()  # iteration start
        self._push_span(f"On iteration end: {event['iteration']}", **event)
        self._pop_span()


    # =========================================================================
    # Candidate Selection and Sampling
    # =========================================================================

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        """Push and pop so span is a leaf (stack stays correct for training_end)."""
        self._push_span(f"On candidate selected: [{event['iteration']}][{event['candidate_idx']}]({event['score']:.2f})", **event)
        self._pop_span()

    def on_minibatch_sampled(self, event: MinibatchSampledEvent) -> None:
        """Push and pop so span is a leaf (otherwise training_end would pop wrong span)."""
        self._push_span(f"On minibatch sampled: [{event['iteration']}]({len(event['minibatch_ids'])}/{event['trainset_size']})", **event)
        self._pop_span()

    # =========================================================================
    # Evaluation Events
    # =========================================================================

    def on_evaluation_start(self, event: EvaluationStartEvent) -> None:
        """Called before evaluating a candidate."""
        self._push_span(f"On evaluation start: [{event['iteration']}][{event['candidate_idx']}](is_seed: {event['is_seed_candidate']})", **event)

    def on_evaluation_end(self, event: EvaluationEndEvent) -> None:
        """Pop evaluation start, then push/pop so evaluation end is a leaf."""
        self._pop_span()  # On evaluation start
        self._push_span(f"On evaluation end: [{event['iteration']}][{event['candidate_idx']}](is_seed: {event['is_seed_candidate']})", **event)
        self._pop_span()

    def on_evaluation_skipped(self, event: EvaluationSkippedEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On evaluation skipped: [{event['iteration']}][{event['candidate_idx']}](is_seed: {event['is_seed_candidate']})", **event)
        self._pop_span()

    def on_valset_evaluation_start(self, event: ValsetEvaluationStartEvent) -> None:
        """Push span so all valset task evals nest under it."""
        label = "seed" if event.get("is_seed") else f"candidate {event.get('candidate_idx', -1)}"
        n = event.get("valset_size")
        suffix = f" [{n} examples]" if n is not None else ""
        self._push_span(
            f"On valset evaluation start: iter {event['iteration']} ({label}){suffix}", **event
        )

    def on_valset_evaluated(self, event: ValsetEvaluatedEvent) -> None:
        """Pop valset eval start, then push/pop valset evaluated (nothing nested under it)."""
        self._pop_span()  # On valset evaluation start
        self._push_span(
            f"On valset evaluated: [{event['iteration']}][{event['candidate_idx']}][avg score: {event['average_score']:.2f}](is_best: {event['is_best_program']})",
            **event,
        )
        self._pop_span()

    # =========================================================================
    # Reflection Events
    # =========================================================================

    def on_reflective_dataset_built(self, event: ReflectiveDatasetBuiltEvent) -> None:
        """Push and pop so span is a leaf (stage: dataset for reflection)."""
        self._push_span(f"On reflective dataset built: [{event['iteration']}][{event['candidate_idx']}]", **event)
        self._pop_span()

    def on_proposal_start(self, event: ProposalStartEvent) -> None:
        """Reflection phase: LLM proposes new candidate. Push so reflection/completion nests under it."""
        self._push_span(f"On reflection (proposal start): [{event['iteration']}](parent: {event['parent_candidate']})", **event)

    def on_proposal_end(self, event: ProposalEndEvent) -> None:
        """Pop reflection span, then push/pop proposal end so nothing nests under it."""
        self._pop_span()  # On reflection (proposal start)
        self._push_span(f"On proposal end: [{event['iteration']}]", **event)
        self._pop_span()

    # =========================================================================
    # Acceptance/Rejection Events
    # =========================================================================

    def on_candidate_accepted(self, event: CandidateAcceptedEvent) -> None:
        """Push and pop so span is a leaf (no spurious nesting)."""
        self._push_span(f"On candidate accepted: [{event['iteration']}][new_idx: {event['new_candidate_idx']}](new_score: {event['new_score']:.2f})", **event)
        self._pop_span()

    def on_candidate_rejected(self, event: CandidateRejectedEvent) -> None:
        """Push and pop so span is a leaf (not nested under budget updated)."""
        self._push_span(f"On candidate rejected: [{event['iteration']}][old_score: {event['old_score']:.2f}](new_score: {event['new_score']:.2f})", **event)
        self._pop_span()

    # =========================================================================
    # Merge Events
    # =========================================================================

    def on_merge_attempted(self, event: MergeAttemptedEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On merge attempted: [{event['iteration']}]", **event)
        self._pop_span()

    def on_merge_accepted(self, event: MergeAcceptedEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On merge accepted: [{event['iteration']}][new_idx: {event['new_candidate_idx']}]", **event)
        self._pop_span()

    def on_merge_rejected(self, event: MergeRejectedEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On merge rejected: [{event['iteration']}]", **event)
        self._pop_span()

    # =========================================================================
    # State Events
    # =========================================================================

    def on_pareto_front_updated(self, event: ParetoFrontUpdatedEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On pareto front updated: [{event['iteration']}])", **event)
        self._pop_span()

    def on_state_saved(self, event: StateSavedEvent) -> None:
        """Push and pop so span is nested under parent with no children."""
        self._push_span(f"On state saved: [{event['iteration']}][{event['run_dir']}]", **event)
        self._pop_span()

    def on_training_start(self, event: TrainingStartEvent) -> None:
        """Training/proposal phase of iteration."""
        self._push_span(f"On training start: {event['iteration']}", **event)

    def on_training_end(self, event: TrainingEndEvent) -> None:
        """End of training/proposal phase."""
        self._pop_span()

    # =========================================================================
    # Budget Tracking
    # =========================================================================

    def on_budget_updated(self, event: BudgetUpdatedEvent) -> None:
        """Push and pop so budget update is a leaf (completion/candidate_rejected don't nest under it)."""
        self._push_span(f"On budget updated: [{event['iteration']}][{event['metric_calls_used']}/{event['metric_calls_remaining']}]", **event)
        self._pop_span()

    # =========================================================================
    # Error Handling
    # =========================================================================

    def on_error(self, event: ErrorEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On error: [{event['iteration']}][{event['exception']}]", **event)
        self._pop_span()
