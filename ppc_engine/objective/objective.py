"""The objective function — one number, lower is better.

This is the ONLY place the scoring rule lives (RULES.md Rule 3, 2026-08-06 spec):

    score = w_ontime · Σ (|miss| − band, capped)²     # the whole objective
          + w_makespan · makespan                      # a strict tie-break only
          + ceiling / committed-promise guards         # dormant in production today
"""

from __future__ import annotations

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics


def _ceiling_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Sum of squared lateness beyond the worst-order ceiling — the barrier that stops
    a re-optimization pushing any order past the current worst-case. 0 when no ceiling."""
    ceiling = config.ceiling_days
    if ceiling is None:
        return 0.0
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = late - ceiling
        if over > 0:
            total += over * over
    return total


def _committed_promise_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Sum of squared committed-order lateness beyond (promised_date + slack). 0 when no
    committed order breaches its promise. Per-order mirror of _ceiling_breach."""
    slack = config.committed_promise_slack_days
    total = 0.0
    for slip in metrics.promise_slip_by_order.values():
        over = slip - slack
        if over > 0:
            total += over * over
    return total


def _ontime_breach(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Squared distance from the delivery date, in EITHER direction, beyond a free
    band and capped. This is the whole objective (2026-08-06 spec).

    `abs()` is the owner's rule that early and late are equally bad. Squaring is the
    owner's rule that misses must be spread: ten orders 6 days out (10 x 2^2 = 40)
    beats one order 30 days out ((30-4)^2 = 676). The cap stops one hopeless order
    swamping the plan.

    `lateness_by_order` is SIGNED — negative means the order finished early.
    """
    band = config.ontime_band_days
    cap = config.ontime_cap_days
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = abs(late) - band
        if over > 0:
            if over > cap:
                over = cap
            total += over * over
    return total


def score(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Score a plan from its metrics. Lower is better."""
    return (
        config.ontime_weight * _ontime_breach(metrics, config)
        + config.ceiling_weight * _ceiling_breach(metrics, config)
        + config.committed_promise_weight * _committed_promise_breach(metrics, config)
        + config.makespan_weight * metrics.makespan_days
    )
