"""The objective — how good a schedule is.

The ONE home of the scoring rule (RULES.md Rule 3, symmetric on-time objective,
2026-08-06): ONE squared, capped, band-tolerant penalty for how far each order
misses its delivery date in either direction, plus a 0.1 makespan tie-break, and
the dormant worst-order-ceiling and committed-promise guards. The scheduler itself
is objective-agnostic; only this package knows what "better" means.
"""

from ppc_engine.objective.metrics import PlanMetrics, compute_metrics, order_lateness_days
from ppc_engine.objective.objective import score

__all__ = ["PlanMetrics", "compute_metrics", "order_lateness_days", "score"]
