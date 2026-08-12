"""When the next process may start — Rule 5, in whole pieces.

The owner's rule, RULES.md:114 and the vendored scheduler's own config docstring
(config.py:109) all say the same thing: overlap 0.8 means the successor starts
once 80 of 100 pieces are done.
The live engine computes ``(1.0 - overlap) * cutting`` instead — the complement —
which agrees only at exactly 50% and is why it survived. At the overlap the
contest has been converging on (88-95) it really starts successors when 5-12
pieces exist, and the piece-flow guard then re-lays the operation later to repair
the impossibility. This module is the correct definition.

Two further rules from RULES.md, both kept:
  * the percentage measures CUTTING time — the setup is excluded, because the
    next machine's own setup runs while this one cuts;
  * a step with no cycle time produces nothing gradually, so its successor waits
    for it to complete.
"""

from __future__ import annotations

import math

from roster_engine.domain import INSPECTION, MACHINING, MANUAL

# Only these kinds pipeline. OS is a vendor block and DISPATCH is a milestone;
# neither hands pieces over gradually.
_INHOUSE = (MACHINING, MANUAL, INSPECTION)


def released_pieces(overlap: float, qty: int) -> int:
    """How many whole pieces must clear before the successor may start.

    Rounded UP: releasing on 5.6 pieces would start a process on a piece that does
    not exist. Never more than the batch, never less than one.
    """
    qty = int(qty)
    if qty <= 0:
        return 0
    p = min(1.0, max(0.0, float(overlap)))
    return max(1, min(qty, int(math.ceil(p * qty))))


def _hands_over_gradually(op) -> bool:
    """Does this step release pieces to its successor as it goes, rather than all
    at once on completion? Only an in-house step that actually cuts (positive
    cycle time) does — OS/DISPATCH are milestones/vendor blocks, and a zero-cycle
    step produces nothing gradually (RULES.md:127)."""
    return op.kind in _INHOUSE and op.cycle_min > 0.0


def overlaps(prev, nxt) -> bool:
    """May ``nxt`` start before ``prev`` has finished?"""
    if prev.kind not in _INHOUSE or nxt.kind not in _INHOUSE:
        return False
    return prev.cycle_min > 0.0


def work_min_before_release(job, op, overlap: float, setup_min: float) -> float:
    """Minutes of WORK on ``op`` after which its successor may start.

    Worked minutes, not wall-clock: an overnight gap must not release pieces that
    were never cut. The caller converts this to a moment by tracking how much the
    machine has actually done.
    """
    qty = job.qty_for(op.seq)
    if qty <= 0:
        return 0.0
    setup = float(setup_min) if op.kind == MACHINING else 0.0
    if not _hands_over_gradually(op):             # no cutting -> no gradual release
        return setup + qty * op.cycle_min
    return setup + released_pieces(overlap, qty) * op.cycle_min
