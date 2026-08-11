"""The non-blocking data-quality report.

Loading never crashes on a data gap (RULES.md "fail loud, fail localized" — the
loader level is the non-blocking one). Instead each gap is recorded here, and the
affected order is simply excluded from scheduling with a clear reason. The report is
surfaced to the user so the underlying master data can be fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GapKind(Enum):
    """Kinds of data gap the loader can find."""

    NO_ROUTING = "no_routing"                  # a sales order's item has no routing
    ROUTING_GAP = "routing_gap"                # a routing step has no machine and isn't OS/dispatch
    PROVISIONAL_MACHINE = "provisional_machine"  # routing references a machine not in the master
    UNSTAFFED_MACHINE = "unstaffed_machine"    # a machine has no qualified operator


@dataclass(frozen=True)
class DataGap:
    """One recorded data-quality issue.

    Attributes:
        kind:   Which kind of gap.
        ref:    The thing it's about (an item code, machine id, or order key text).
        detail: Human-readable explanation.
    """

    kind: GapKind
    ref: str
    detail: str


@dataclass
class DataReport:
    """All data gaps found during a load, plus which orders were blocked by them."""

    gaps: list[DataGap] = field(default_factory=list)
    # order key (so_no, item_code) -> reason it can't be scheduled
    blocked_orders: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, kind: GapKind, ref: str, detail: str) -> None:
        self.gaps.append(DataGap(kind, ref, detail))

    def block_order(self, key: tuple[str, str], reason: str) -> None:
        self.blocked_orders[key] = reason

    def summary(self) -> dict[str, int]:
        """Count of gaps by kind (for a quick banner)."""
        out: dict[str, int] = {}
        for g in self.gaps:
            out[g.kind.value] = out.get(g.kind.value, 0) + 1
        return out
