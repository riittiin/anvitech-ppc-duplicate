"""Multi-start optimization contest — the "spend compute, keep the best" engine.

WHY THIS EXISTS
    The convergence study (OPTIMIZATION.md §7f) measured two hard facts about the
    sequence optimizer on the real book:
      1. A *single* search converges by ~950 plans — past that one seed just spins.
      2. But the SEED you start from matters MORE than how long you run it: independent
         seeds spread ~4% in final score, and a lucky seed at 600 plans beat an unlucky
         one at 1500. Multi-start (many seeds, keep the best) is the real lever.
    So the honest way to approach the minimum is **best-of-K over many seeds**, at every
    (overlap, consolidation) candidate — not one long run. Job-shop scheduling is
    NP-hard: nothing here *proves* the global optimum. Instead we drive toward it and
    *measure* convergence by watching the best-of-K curve flatten (see ``best_of_k_curve``).

WHAT IT DOES
    For each (overlap, consolidation) combo it runs the sequence optimizer once per seed,
    keeps that combo's best plan, and records every seed's score in order — so the caller
    can see how many seeds were "enough" (the best-of-K curve stops dropping). The overall
    winner is the lowest-scoring plan across the whole grid.

WHERE IT RUNS
    Pure and deterministic, so the SAME contest runs three ways with identical results:
      - in-process (``services.jobs`` — the app's Optimize button),
      - a local orchestrator (``tools/optimize_local.py``),
      - a GitHub-Actions matrix, one job per (overlap, consolidation, seed), whose JSON
        results are recombined by ``reduce_results`` (``tools/optimize_reduce.py``).
    One combo+seed is one independent unit of work → it fans out perfectly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.optimize.search import Sequence, optimize

# Default overlap grid — the full fine sweep the owner asked for (0.50→0.90 by 0.05),
# each value fully searched (no cheap outer loop). 0.0 (sequential) is added by the
# caller/CLI when a fair no-overlap baseline is wanted; the app default sweep is the
# pipelining region the data favours.
DEFAULT_OVERLAPS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)

# Default consolidation windows (days). 0.0 (no merging) is always the fair baseline.
DEFAULT_CONSOLIDATIONS = (0.0,)


@dataclass
class ComboResult:
    """The outcome of one (overlap, consolidation) candidate across all its seeds.

    Attributes:
        overlap:        the operation-overlap this combo used.
        consolidation:  the consolidation window (days) this combo used.
        seed_scores:    each seed's best objective score, IN SEED ORDER. The running
                        minimum of this list is the best-of-K curve — flat tail ⇒ more
                        seeds stopped helping (enough compute for this combo).
        best_score:     min over ``seed_scores`` — this combo's achieved score.
        best_seed:      the seed that produced ``best_score``.
        best_sequence:  the winning order sequence for this combo.
        best_metrics:   its metrics (tardiness, makespan, …).
        evaluations:    total distinct schedule evaluations spent on this combo.
    """

    overlap: float
    consolidation: float
    seed_scores: list[float]
    best_score: float
    best_seed: int
    best_sequence: Sequence
    best_metrics: PlanMetrics
    evaluations: int


@dataclass
class OffloadResult:
    """The winning plan across the whole contest, plus every combo's detail for reporting.

    Attributes:
        best_overlap / best_consolidation: the knobs to reproduce the winning plan
                        (decode with ``replace(config, overlap=…, consolidation_window=…)``).
        best_sequence / best_score / best_metrics: the winning plan.
        evaluations:    total schedule evaluations across the entire contest.
        combos:         one ComboResult per (overlap, consolidation), for reporting the
                        overlap curve and the per-combo best-of-K convergence.
    """

    best_overlap: float
    best_consolidation: float
    best_sequence: Sequence
    best_score: float
    best_metrics: PlanMetrics
    evaluations: int
    combos: list[ComboResult]


def best_of_k_curve(seed_scores: list[float]) -> list[float]:
    """Running minimum of the seed scores = "best score after K seeds", for K=1..N.

    A flat tail means extra seeds stopped finding anything better — the empirical signal
    that we've spent enough compute on this candidate (the honest stand-in for "reached
    the minimum", which is unprovable for an NP-hard shop schedule)."""
    out: list[float] = []
    run = float("inf")
    for s in seed_scores:
        run = min(run, s)
        out.append(run)
    return out


def run_offload(
    orders: list[Order],
    masters: Masters,
    config: PlanConfig,
    overlaps: tuple[float, ...] = DEFAULT_OVERLAPS,
    consolidations: tuple[float, ...] = DEFAULT_CONSOLIDATIONS,
    seeds: tuple[int, ...] = tuple(range(8)),
    budget: int = 700,
    on_result: Callable[[float, float, int, float], None] | None = None,
) -> OffloadResult:
    """Run the full multi-start contest in-process and return the overall best plan.

    Args:
        orders:          the (schedulable) demand to plan.
        masters:         the shop.
        config:          base plan config (overlap/consolidation are overridden per combo).
        overlaps:        overlap values to contest — each fully searched.
        consolidations:  consolidation windows (days) to contest.
        seeds:           RNG seeds to run PER combo; best-of-these is the combo's score.
                         More seeds → closer to the true minimum (the owner's "spend
                         compute" lever). Deterministic for fixed args.
        budget:          schedule-evaluation budget PER (combo, seed) run. ~700 sits just
                         past the measured convergence knee for one seed (§7f).
        on_result:       optional progress hook called (overlap, consolidation, seed,
                         score) after every single run — used to stream a live curve.

    Returns:
        An OffloadResult: the winning (overlap, consolidation, sequence) and, for every
        combo, the seed-by-seed scores so the caller can prove the best-of-K curve flattened.
    """
    combos: list[ComboResult] = []
    total_evals = 0
    for ov in overlaps:
        for cw in consolidations:
            cfg = replace(config, overlap=ov, consolidation_window=cw)
            seed_scores: list[float] = []
            best_score = float("inf")
            best_seed = seeds[0] if seeds else 0
            best_sequence: Sequence = []
            best_metrics: PlanMetrics | None = None
            combo_evals = 0
            for sd in seeds:
                res = optimize(orders, masters, cfg, budget=budget, seed=sd)
                combo_evals += res.evaluations
                total_evals += res.evaluations
                seed_scores.append(res.best_score)
                if res.best_score < best_score:
                    best_score = res.best_score
                    best_seed = sd
                    best_sequence = res.best_sequence
                    best_metrics = res.best_metrics
                if on_result:
                    on_result(ov, cw, sd, res.best_score)
            combos.append(ComboResult(
                overlap=ov, consolidation=cw, seed_scores=seed_scores,
                best_score=best_score, best_seed=best_seed,
                best_sequence=best_sequence, best_metrics=best_metrics,
                evaluations=combo_evals,
            ))

    return _pick_winner(combos, total_evals)


def _pick_winner(combos: list[ComboResult], total_evals: int) -> OffloadResult:
    """Choose the overall best combo (lowest score; ties → less overlap, then less
    consolidation — the simpler plan). Shared by the in-process run and the cloud reduce."""
    best = min(combos, key=lambda c: (c.best_score, c.overlap, c.consolidation))
    return OffloadResult(
        best_overlap=best.overlap,
        best_consolidation=best.consolidation,
        best_sequence=best.best_sequence,
        best_score=best.best_score,
        best_metrics=best.best_metrics,
        evaluations=total_evals,
        combos=combos,
    )


def metrics_summary(m: PlanMetrics | None) -> dict:
    """The JSON-safe subset of PlanMetrics carried across the worker→reducer boundary
    (the per-order lateness map is dropped — reporting numbers only)."""
    if m is None:
        return {}
    return {
        "total_tardiness_days": m.total_tardiness_days,
        "max_tardiness_days": m.max_tardiness_days,
        "late_order_count": m.late_order_count,
        "makespan_days": m.makespan_days,
    }


def reduce_results(rows: list[dict]) -> dict:
    """Recombine per-(overlap, consolidation, seed) worker JSON into the contest winner.

    Each row is one cloud matrix job's output:
        {overlap, consolidation, seed, score, sequence, evaluations, metrics?}
    Rows are grouped into combos (seed scores gathered in seed order), the winner picked
    exactly as the in-process run picks it, and per-combo best-of-K curves attached.

    Returns a JSON-safe summary dict (NOT an OffloadResult — the reducer has no live
    PlanMetrics objects, only the numbers the workers reported):
        {best: {overlap, consolidation, score, sequence, metrics, seed},
         evaluations, combos: [{overlap, consolidation, seed_scores, best_of_k,
                                best_score, best_seed, evaluations}]}
    """
    # Group rows by (overlap, consolidation), remembering each seed's score + payload.
    grouped: dict[tuple[float, float], list[dict]] = {}
    for r in rows:
        key = (float(r["overlap"]), float(r["consolidation"]))
        grouped.setdefault(key, []).append(r)

    combos = []
    best_row = None
    total_evals = 0
    for (ov, cw), rs in sorted(grouped.items()):
        rs_sorted = sorted(rs, key=lambda r: int(r["seed"]))
        seed_scores = [float(r["score"]) for r in rs_sorted]
        combo_best = min(rs_sorted, key=lambda r: float(r["score"]))
        combo_evals = sum(int(r.get("evaluations", 0)) for r in rs_sorted)
        total_evals += combo_evals
        combos.append({
            "overlap": ov,
            "consolidation": cw,
            "seed_scores": seed_scores,
            "best_of_k": best_of_k_curve(seed_scores),
            "best_score": float(combo_best["score"]),
            "best_seed": int(combo_best["seed"]),
            "evaluations": combo_evals,
        })
        # Overall winner: lowest score, ties → less overlap, then less consolidation.
        if best_row is None or (float(combo_best["score"]), ov, cw) < (
            float(best_row["score"]), float(best_row["overlap"]), float(best_row["consolidation"])
        ):
            best_row = combo_best

    best = None
    if best_row is not None:
        best = {
            "overlap": float(best_row["overlap"]),
            "consolidation": float(best_row["consolidation"]),
            "seed": int(best_row["seed"]),
            "score": float(best_row["score"]),
            "sequence": [list(k) for k in best_row["sequence"]],
            "metrics": best_row.get("metrics", {}),
        }
    return {"best": best, "evaluations": total_evals, "combos": combos}
