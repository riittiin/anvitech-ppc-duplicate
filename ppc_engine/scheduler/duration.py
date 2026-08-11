"""How long one operation takes — the single source of the duration formula.

Straight from RULES.md "Process duration":
  - Machining (CNC/VMC):      90-min setup + (qty × cycle)
  - Manual / inspection:      qty × cycle          (no setup)
  - Outsourced (OS):          the cycle value itself (a fixed lead time; not ×qty)
  - Dispatch:                 0 (a milestone)
"""

from __future__ import annotations

from ppc_engine.config import PlanConfig
from ppc_engine.domain.routing import Operation, OperationKind


def operation_duration_min(op: Operation, qty: int, config: PlanConfig) -> float:
    """Return the duration of ``op`` for a batch of ``qty`` pieces, in minutes.

    Args:
        op:     The operation (its kind and cycle_min).
        qty:    Batch quantity (the order's SO Delivery Qty, or this op's remaining on a
                re-plan). ``<= 0`` means the op is already finished → 0 time (no setup).
        config: For the machining setup time.
    """
    if qty <= 0:
        return 0.0  # already done — a re-plan schedules it as a zero-time milestone
    if op.kind == OperationKind.MACHINING:
        # One setup for the whole batch, then per-piece cutting.
        return config.setup_min + qty * op.cycle_min
    if op.kind in (OperationKind.MANUAL, OperationKind.INSPECTION):
        # Per-piece work, no setup.
        return qty * op.cycle_min
    if op.kind == OperationKind.OUTSOURCED:
        # A fixed off-site lead time (the "thousands" cycle value), not multiplied.
        return op.cycle_min
    # DISPATCH — a zero-duration milestone.
    return 0.0
