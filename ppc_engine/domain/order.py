"""Sales orders — the demand we schedule.

One Order = one line of the SO list. We plan against **SO Delivery Qty** (RULES.md
Part 1 §5). A sales-order number alone is NOT unique — the same SO# can carry several
item lines — so the identity of an order is the (so_no, item_code) pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Order:
    """A single sales-order line to be produced.

    Attributes:
        so_no:      Sales-order number, e.g. ``"26-27SO128"``.
        item_code:  Item code — links to a Routing.
        item_name:  Item name (for display).
        qty:        The quantity to produce = SO Delivery Qty (headline remaining on a
                    re-plan).
        due_date:   The delivery date (the due date lateness is measured against).
        process_remaining: On a RE-PLAN after production, ``{op.seq → remaining qty at
                    that step}`` — so each operation is scheduled at its OWN remaining
                    (a finished step gets remaining 0 → scheduled as a zero-time
                    milestone, no re-do, no phantom setup). ``None`` on a fresh plan,
                    where every op runs the full ``qty`` (byte-identical to before).
                    Excluded from equality/hash (Orders are keyed by ``.key``).
        promise_date: The committed-date snapshot for this order (informational —
                    see CLAUDE.md's Phase-2 pivot: lanes/promises are display-only,
                    never scheduling constraints). ``None`` on a fresh/uncommitted
                    order — byte-identical to before. Excluded from equality/hash.
    """

    so_no: str
    item_code: str
    item_name: str
    qty: int
    due_date: date
    process_remaining: dict | None = field(default=None, compare=False)
    promise_date: date | None = field(default=None, compare=False)

    @property
    def key(self) -> tuple[str, str]:
        """The unique identity of this order line: (so_no, item_code)."""
        return (self.so_no, self.item_code)
