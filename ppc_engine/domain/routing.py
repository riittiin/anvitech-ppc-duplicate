"""Routings — the ordered recipe of processes that makes an item.

One item has one Routing = an ordered list of Operations (Process 1, 2, 3 …), always
ending in DISPATCH. See RULES.md Part 1 §4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OperationKind(Enum):
    """What kind of step an operation is — decides its resources and its duration
    formula (see scheduler/duration.py and RULES.md).

    - MACHINING  : runs on a CNC/VMC + operator.       dur = setup + qty×cycle
    - MANUAL     : runs on a manual station + helper.  dur = qty×cycle
    - INSPECTION : runs on an MI/CMM station + inspector. dur = qty×cycle
    - OUTSOURCED : leaves the shop ("OS"). No in-house resource.
                   dur = the cycle value itself (a fixed lead time), run continuously.
    - DISPATCH   : the terminal "order shipped" milestone. No resource, zero duration.
    """

    MACHINING = "machining"
    MANUAL = "manual"
    INSPECTION = "inspection"
    OUTSOURCED = "outsourced"
    DISPATCH = "dispatch"


@dataclass(frozen=True)
class Operation:
    """One step in a routing.

    Attributes:
        seq:            1-based position in the routing (Process 1 → seq=1).
        name:           The raw step name, e.g. ``"CNC FIRST SIDE"`` (for display).
        kind:           OperationKind (above).
        machine_options: Canonical machine ids that can do this step, in preference
                        order (Allotted first, else Suggested). Empty for OUTSOURCED /
                        DISPATCH. A ``/`` in the sheet means alternatives, e.g.
                        ``["CNC3", "CNC6"]`` or ``["MW1","MW2","MW3"]``.
        cycle_min:      Per-piece cycle time in minutes for in-house steps; for
                        OUTSOURCED it is the whole-activity lead time in minutes.
                        0 for DISPATCH.
    """

    seq: int
    name: str
    kind: OperationKind
    machine_options: tuple[str, ...] = ()
    cycle_min: float = 0.0


@dataclass(frozen=True)
class Routing:
    """An item's full recipe.

    Attributes:
        item_code:   The item code (links from a sales order).
        description: Item description (for display).
        operations:  The ordered operations (seq 1..N, ending in DISPATCH).
    """

    item_code: str
    description: str
    operations: tuple[Operation, ...] = field(default_factory=tuple)
