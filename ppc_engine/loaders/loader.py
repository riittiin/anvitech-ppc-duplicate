"""load_all — the loaders' public entry point.

Reads a workbook into a Masters + Orders + DataReport, then works out which orders are
actually schedulable (routing present, complete, and every in-house step has a staffed
machine). Unschedulable orders are recorded with a reason and excluded — the schedule
is built only from what can genuinely run (fail-localized, RULES.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import ROLE_FOR_KIND
from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders.masters_loader import (
    load_calendar,
    load_machines,
    load_operators,
    load_routings,
    register_provisional_machines,
)
from ppc_engine.loaders.report import DataReport
from ppc_engine.loaders.sales_orders import load_orders
from ppc_engine.loaders.workbook import open_workbook


@dataclass
class LoadResult:
    """Everything a load produces."""

    masters: Masters
    orders: list[Order]
    report: DataReport

    def schedulable_orders(self) -> list[Order]:
        """Orders that can actually be scheduled (not blocked in the report)."""
        blocked = self.report.blocked_orders
        return [o for o in self.orders if o.key not in blocked]


def _staffed_machines(masters: Masters) -> set[str]:
    """Machine ids somebody in Settings is assigned to.

    Role is NOT a gate here either (2026-08-07, same fix as
    ``scheduler.staffing.build_machine_pools``) — and the blast radius of getting it
    wrong is larger: an "unstaffed" machine's orders are BLOCKED as unschedulable, so a
    machine covered only by a role-mismatched person silently took its whole order book
    out of the plan."""
    staffed: set[str] = set()
    for mid in masters.machines:
        if any(mid in op.qualified_machines for op in masters.operators):
            staffed.add(mid)
    return staffed


def _block_unschedulable(masters: Masters, orders: list[Order], report: DataReport) -> None:
    """Record, per order, whether it can be scheduled and why not."""
    from ppc_engine.loaders.report import GapKind

    staffed = _staffed_machines(masters)
    routing_gap_items = {g.ref for g in report.gaps if g.kind == GapKind.ROUTING_GAP}

    for order in orders:
        routing = masters.routings.get(order.item_code)
        if routing is None:
            report.add(GapKind.NO_ROUTING, order.item_code, f"order {order.so_no} has no routing for its item")
            report.block_order(order.key, "no routing for item")
            continue
        if order.item_code in routing_gap_items:
            report.block_order(order.key, "routing has a step with no machine")
            continue
        # Every in-house step must have at least one staffed machine option.
        for op in routing.operations:
            if op.kind in (OperationKind.MACHINING, OperationKind.MANUAL, OperationKind.INSPECTION):
                if not any(m in staffed for m in op.machine_options):
                    report.block_order(
                        order.key,
                        f"step '{op.name}' has no staffed machine ({list(op.machine_options)})",
                    )
                    break


def load_all(path, flexible_machines: bool = False) -> LoadResult:
    """Load a workbook at ``path`` into a LoadResult.

    ``flexible_machines`` (see load_routings): False = machining ops locked to their
    Allotted machine; True = ops may use any machine in their Suggested set (the
    machine-flexibility lever, OPTIMIZATION.md).
    """
    wb = open_workbook(path)
    report = DataReport()

    machines = load_machines(wb)
    operators = load_operators(wb)
    calendar = load_calendar(wb)
    routings = load_routings(wb, report, flexible_machines=flexible_machines)
    register_provisional_machines(machines, routings, report)

    masters = Masters(machines=machines, operators=operators, routings=routings, calendar=calendar)
    orders = load_orders(wb)
    _block_unschedulable(masters, orders, report)

    return LoadResult(masters=masters, orders=orders, report=report)
