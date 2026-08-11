"""Domain model — the pure data the engine reasons about.

These are plain dataclasses and enums with NO presentation logic (no date
formatting, no row-building). Keeping the domain pure means a display change never
forces a domain change (a trap from the old build — see LESSONS.md).

Re-exported here so callers can write ``from ppc_engine.domain import Machine, Order``.
"""

from ppc_engine.domain.resources import (
    Machine,
    MachineKind,
    Operator,
    Role,
    Shift,
)
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.domain.order import Order
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters

__all__ = [
    "Machine",
    "MachineKind",
    "Operator",
    "Role",
    "Shift",
    "Operation",
    "OperationKind",
    "Routing",
    "Order",
    "ShopCalendar",
    "Masters",
]
