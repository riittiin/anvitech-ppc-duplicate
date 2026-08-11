"""The scheduler — the ONE decoder that turns an order sequence into a real schedule.

Pure and deterministic: ``decode(orders, sequence, masters, config) -> Schedule``.
This is the truth-teller the whole system is measured against (OPTIMIZATION.md).
"""

from ppc_engine.scheduler.flow_scheduler import decode
from ppc_engine.scheduler.schedule import Schedule, Segment, FrozenOp

__all__ = ["decode", "Schedule", "Segment", "FrozenOp"]
