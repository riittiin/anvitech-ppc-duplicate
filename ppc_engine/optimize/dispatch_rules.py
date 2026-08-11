"""Dispatch rules — fast, sensible starting sequences (the search's seeds).

Each returns a list of order keys in priority order. They are cheap heuristics: good
warm starts for the local search, and useful baselines in their own right.
"""

from __future__ import annotations

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.scheduler.duration import operation_duration_min

# CNC/VMC daily capacity, used to turn work-minutes into a rough "work-days" figure
# for the slack heuristic. (Ordering heuristic only — not a scheduling number.)
_MIN_PER_DAY = 19.5 * 60.0


def work_minutes(order: Order, masters: Masters, config: PlanConfig) -> float:
    """Total in-house + outsourced work minutes for an order (sum over its routing)."""
    routing = masters.routings.get(order.item_code)
    if routing is None:
        return 0.0
    return sum(operation_duration_min(op, order.qty, config) for op in routing.operations)


def edd_sequence(orders: list[Order]) -> list[tuple[str, str]]:
    """Earliest Due Date first — the natural on-time-delivery baseline."""
    return [o.key for o in sorted(orders, key=lambda o: (o.due_date, o.key))]


def spt_sequence(orders: list[Order], masters: Masters, config: PlanConfig) -> list[tuple[str, str]]:
    """Shortest Processing Time first — clears many small orders early."""
    return [o.key for o in sorted(orders, key=lambda o: (work_minutes(o, masters, config), o.key))]


def lpt_sequence(orders: list[Order], masters: Masters, config: PlanConfig) -> list[tuple[str, str]]:
    """Longest Processing Time (total flow, incl. outsourcing) first.

    Front-loads the orders with the longest end-to-end routing so their long tails
    (big machining + multi-day OS steps) run concurrently with everyone else's work
    instead of straggling at the end and stretching the makespan. Due date breaks ties.
    """
    return [
        o.key
        for o in sorted(orders, key=lambda o: (-work_minutes(o, masters, config), o.due_date, o.key))
    ]


def slack_sequence(orders: list[Order], masters: Masters, config: PlanConfig) -> list[tuple[str, str]]:
    """Least slack first, where slack ≈ days-until-due − rough work-days.

    A minimum-slack ordering prioritises orders that are most at risk of being late —
    a strong seed for a tardiness objective.
    """
    start = config.plan_start.date()

    def slack(o: Order) -> float:
        days_to_due = (o.due_date - start).days
        work_days = work_minutes(o, masters, config) / _MIN_PER_DAY
        return days_to_due - work_days

    return [o.key for o in sorted(orders, key=lambda o: (slack(o), o.key))]
