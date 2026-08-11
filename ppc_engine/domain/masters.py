"""Masters — the whole shop definition bundled into one object.

Everything the scheduler needs to know about the *shop* (as opposed to the *demand*,
which is the list of Orders). Passed read-only into the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.resources import Machine, Operator
from ppc_engine.domain.routing import Routing


@dataclass(frozen=True)
class Masters:
    """A read-only snapshot of the shop.

    Attributes:
        machines:  Map of canonical machine id → Machine.
        operators: All people (operators, helpers, inspectors).
        routings:  Map of item_code → Routing.
        calendar:  The shop calendar (off-days, holidays, leave).
    """

    machines: dict[str, Machine] = field(default_factory=dict)
    operators: tuple[Operator, ...] = field(default_factory=tuple)
    routings: dict[str, Routing] = field(default_factory=dict)
    calendar: ShopCalendar = field(default_factory=ShopCalendar)
