"""The optimizer — search over order sequences to minimise the objective.

Given the shop + orders, it tries many order sequences (each decoded by the one
scheduler and scored by the one objective) and keeps the best. This is the layer that
turns "a valid schedule" into "a good schedule" (OPTIMIZATION.md Layer 2).

Public entry point: ``optimize(...) -> OptimizeResult``.
"""

from ppc_engine.optimize.search import OptimizeResult, optimize
from ppc_engine.optimize.memetic import memetic
from ppc_engine.optimize.contest import (
    ContestResult,
    TuneResult,
    optimize_overlap,
    tune_consolidation,
    tune_overlap,
)
from ppc_engine.optimize.offload import (
    ComboResult,
    OffloadResult,
    best_of_k_curve,
    metrics_summary,
    reduce_results,
    run_offload,
)
from ppc_engine.optimize.dispatch_rules import edd_sequence, slack_sequence, spt_sequence

__all__ = [
    "optimize",
    "memetic",
    "optimize_overlap",
    "tune_overlap",
    "tune_consolidation",
    "run_offload",
    "reduce_results",
    "best_of_k_curve",
    "metrics_summary",
    "OptimizeResult",
    "ContestResult",
    "TuneResult",
    "OffloadResult",
    "ComboResult",
    "edd_sequence",
    "spt_sequence",
    "slack_sequence",
]
