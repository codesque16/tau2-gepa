"""Evaluation context for GEPA-aware evaluators (e.g. tau2 gepa_eval).

When an evaluator is run inside optimize_anything(), a log context is active.
This module provides get_gepa_eval_context() so downstream runners (e.g. tau2's
evaluate_for_gepa) can receive that context and nest their spans/logs under
GEPA's evaluation span.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gepa.optimize_anything import LogContext


@dataclass
class GepaEvalContext:
    """Context passed from GEPA into an evaluator run (e.g. tau2 gepa_eval).

    When the evaluator is invoked by optimize_anything(), log_context is the
    active per-call log buffer. Downstream code can use it to attach output
    (e.g. via oa.log()) or to nest tracing spans under the current evaluation.
    When not inside an evaluator, log_context is None.

    Implements .get(key, default=None) so consumers (e.g. tau2) that expect a
    dict-like context (iteration, split, candidate_idx, eval_type, minibatch_size)
    can call gepa_context.get(...) without error. Those keys are not yet
    populated; they return default until the engine passes them through.
    """

    log_context: LogContext | None

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like get for compatibility with tau2 and other consumers."""
        if key == "log_context":
            return self.log_context
        return default


def get_gepa_eval_context() -> GepaEvalContext:
    """Return the current GEPA evaluation context for this thread.

    Call this from inside an evaluator (or code invoked by it) to obtain the
    active log context so that external runners (e.g. tau2's evaluate_for_gepa)
    can nest their spans and attach logs under the current GEPA evaluation.
    When not inside an evaluator, log_context will be None.
    """
    from gepa.optimize_anything import _get_log_context

    return GepaEvalContext(log_context=_get_log_context())
