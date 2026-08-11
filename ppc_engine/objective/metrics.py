"""Metrics — measure a schedule against the due dates.

Lateness is measured in **days**: an order's completion date minus its due date. A
negative value (early/on-time) is clamped to 0 for *tardiness*. These feed the
objective (objective.py) and the human-facing reports later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ppc_engine.domain.order import Order
from ppc_engine.scheduler.schedule import Schedule


def order_lateness_days(completion: datetime, due) -> float:
    """Days late = completion date − due date. Negative means finished early.

    We compare *dates* (a delivery due date has no time-of-day), so completing any
    time on the due date counts as 0 days late.
    """
    return (completion.date() - due).days


@dataclass(frozen=True)
class PlanMetrics:
    """Everything the objective needs, plus a few reporting numbers.

    Attributes:
        total_tardiness_days: Σ over orders of max(0, days late). The primary number
                              (RULES.md Rule 3) — every late day on every order counts.
        max_tardiness_days:   The single worst order's days late — the fairness /
                              no-starvation signal.
        late_order_count:     How many orders are late at all (reporting only, NOT the
                              objective — we never minimise the *count*, LESSONS.md).
        makespan_days:        Span from plan start to the last order's completion.
        lateness_by_order:    Per-order signed lateness in days (for reports).
        promise_slip_by_order: Per-order signed days vs ``Order.promise_date``
                              (completion date − promise date), only for orders that
                              carry a promise date. Empty dict when no order has one
                              — byte-identical to before.
    """

    total_tardiness_days: float
    max_tardiness_days: float
    late_order_count: int
    makespan_days: float
    lateness_by_order: dict[tuple[str, str], float]
    promise_slip_by_order: dict[tuple[str, str], float] = field(default_factory=dict)


def compute_metrics(schedule: Schedule, orders: list[Order], plan_start: datetime) -> PlanMetrics:
    """Compute the metrics for ``schedule`` against ``orders``' due dates."""
    due_by_key = {o.key: o.due_date for o in orders}
    promise_by_key = {o.key: o.promise_date for o in orders if o.promise_date is not None}

    lateness_by_order: dict[tuple[str, str], float] = {}
    promise_slip_by_order: dict[tuple[str, str], float] = {}
    total_tardiness = 0.0
    max_tardiness = 0.0
    late_count = 0
    for key, completion in schedule.completion.items():
        late = order_lateness_days(completion, due_by_key[key])
        lateness_by_order[key] = late
        tardiness = max(0.0, late)
        total_tardiness += tardiness
        if tardiness > max_tardiness:
            max_tardiness = tardiness
        if tardiness > 0:
            late_count += 1
        if key in promise_by_key:
            promise_slip_by_order[key] = (completion.date() - promise_by_key[key]).days

    end = schedule.makespan_end()
    makespan_days = ((end - plan_start).total_seconds() / 86400.0) if end else 0.0

    return PlanMetrics(
        total_tardiness_days=total_tardiness,
        max_tardiness_days=max_tardiness,
        late_order_count=late_count,
        makespan_days=round(makespan_days, 4),
        lateness_by_order=lateness_by_order,
        promise_slip_by_order=promise_slip_by_order,
    )
