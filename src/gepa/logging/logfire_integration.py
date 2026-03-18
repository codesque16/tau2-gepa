"""Logfire span callback for GEPA optimization hierarchy.

Creates nested spans around iterations, minibatches, and evaluations so that
downstream spans (e.g. gepa_eval from tau2) are properly nested and easier
to understand in Logfire. Captures candidate evolution: current_candidate_set,
pareto sets with scores, reject/accept, and tree-based parent-child lineage.
"""

import hashlib
import json
from typing import Any
from gepa.core.callbacks import (
    BudgetExhaustedEvent,
    BudgetUpdatedEvent,
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
    OptimizationEndEvent,
    OptimizationStartEvent,
    ParetoFrontUpdatedEvent,
    ProposalEndEvent,
    ProposalStartEvent,
    ReflectiveDatasetBuiltEvent,
    StateSavedEvent,
    TrainingEndEvent,
    TrainingStartEvent,
    ValsetEvaluatedEvent,
    ValsetEvaluationStartEvent,
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
        # Drop raw GEPAState object from span attributes; keep only serializable fields.
        event_no_state = {k: v for k, v in event.items() if k != "state"}
        self._push_span(f"On iteration start: {event['iteration']}", **event_no_state)

    def on_iteration_end(self, event: IterationEndEvent) -> None:
        self._pop_span()  # iteration start
        best = event.get("best_program_as_per_agg_score_valset")
        score = event.get("best_score_on_valset")
        pareto_agg = event.get("valset_pareto_front_agg")
        extra = ""
        if best is not None and score is not None and pareto_agg is not None:
            extra = f" best_prog={best} best_score={score:.2f} pareto_agg={pareto_agg:.2f}"
        event_no_state = {k: v for k, v in event.items() if k != "state"}
        self._push_span(
            f"On iteration end: {event['iteration']}{extra}",
            **event_no_state,
        )
        self._pop_span()


    # =========================================================================
    # Candidate Selection and Sampling
    # =========================================================================

    def on_candidate_selected(self, event: CandidateSelectedEvent) -> None:
        """Push and pop so span is a leaf (stack stays correct for training_end)."""
        dist = event.get("selection_distribution")
        dist_str = f" probs={dist}" if dist else ""
        candidate = event.get("candidate") or {}
        candidate_sha256 = hashlib.sha256(
            json.dumps(sorted(candidate.items()), default=str, ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        # Candidate is a dict of instruction/prompt components; log the most relevant text we can find.
        prompt_text = (
            candidate.get("prompt")
            or candidate.get("instructions")
            or candidate.get("instruction")
            or candidate.get("content")
            or ""
        )
        if not prompt_text and isinstance(candidate, dict) and candidate:
            # Fallback: log all components in a stable order.
            prompt_text = "\n\n".join(f"{k}: {v}" for k, v in sorted(candidate.items()))
        # Avoid huge spans if prompts are very long.
        if isinstance(prompt_text, str) and len(prompt_text) > 8000:
            prompt_text = prompt_text[:8000] + "\n\n...[truncated]"
        self._push_span(
            f"On candidate selected: [{event['iteration']}][{event['candidate_idx']}]"
            f"({event['score']:.2f}) sha256={candidate_sha256[:12]}{dist_str}",
            candidate_sha256=candidate_sha256,
            prompt_selected_to_mutate=prompt_text,
            **event,
        )
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
        self._push_span(
            f"On reflection (proposal start): [{event['iteration']}]",
            **event,
        )

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
        old = event.get("old_score")
        if old is not None:
            msg = (
                f"On candidate accepted: [{event['iteration']}][new_idx: {event['new_candidate_idx']}]"
                f"(old_score: {old:.2f} -> new_score: {event['new_score']:.2f})"
            )
        else:
            msg = (
                f"On candidate accepted: [{event['iteration']}][new_idx: {event['new_candidate_idx']}]"
                f"(new_score: {event['new_score']:.2f})"
            )
        self._push_span(msg, **event)
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
        used = event.get("metric_calls_used", 0)
        remaining = event.get("metric_calls_remaining")
        total = used + (remaining or 0)
        label = f"{used}/{total}" if total else f"{used}/?"
        self._push_span(
            f"On budget updated: [{event['iteration']}][{label}]",
            **event,
        )
        self._pop_span()

    def on_budget_exhausted(self, event: BudgetExhaustedEvent) -> None:
        """Log seed, best candidate, and all Pareto frontier programs with scores where they perform best."""
        best_idx = event["best_candidate_idx"]
        best_score = event["best_score_on_valset"]
        pareto_ids = event["pareto_front_program_ids"]
        per_prog = event["per_program_best_val_scores"]
        # Human-readable summary: for each program on frontier, count and mean score where it's best
        summary_parts = [
            f"best_candidate_idx={best_idx} best_score={best_score:.2f}",
            f"pareto_programs={pareto_ids}",
        ]
        for prog_idx in pareto_ids:
            scores = per_prog.get(prog_idx, {})
            n = len(scores)
            avg = sum(scores.values()) / n if n else 0.0
            summary_parts.append(f"P{prog_idx}: best_on_{n}_vals avg={avg:.2f}")
        self._push_span(
            "On budget exhausted: " + " | ".join(summary_parts),
            seed_candidate=event["seed_candidate"],
            best_candidate=event["best_candidate"],
            best_candidate_idx=best_idx,
            best_score_on_valset=best_score,
            total_metric_calls=event["total_metric_calls"],
            total_iterations=event["total_iterations"],
            pareto_front_program_ids=pareto_ids,
            per_program_best_val_scores=per_prog,
        )
        self._pop_span()

    # =========================================================================
    # Error Handling
    # =========================================================================

    def on_error(self, event: ErrorEvent) -> None:
        """Push and pop so span is a leaf."""
        self._push_span(f"On error: [{event['iteration']}][{event['exception']}]", **event)
        self._pop_span()
