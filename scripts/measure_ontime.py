"""Ship gate for the symmetric on-time objective (spec 2026-08-06 §6).

Judged on the OWNER'S measures, not on score — the two objectives are not
comparable by score, so only outcomes count:

    orders inside +/-4 days   must NOT fall
    worst single order        must NOT get worse
    at least one              must strictly improve

    python3 scripts/measure_ontime.py Test9.xlsx --budget 400
"""
import argparse
import sys
from dataclasses import replace
from datetime import date

sys.path.insert(0, ".")

from engine import loaders, new_engine, optimizer
from engine.config import Config
from engine.models import PlanRun
from engine.optimizer import ONTIME_BAND_DAYS as BAND
from engine.pipeline import run_forward
import ppc_engine.optimize.search as _search
from ppc_engine.objective.objective import _ceiling_breach, _committed_promise_breach

SEEDS = (42, 7, 2026)

# ---------------------------------------------------------------------------
# The OLD objective CANNOT be recovered by zeroing the new weight: Tasks 1-2
# deleted total_tardiness_days, _severity and the fairness term outright, so
# ontime_weight=0 leaves only makespan — pure makespan minimisation, which has
# never run. A faithful baseline must reconstruct the pre-branch formula.
# Values below are the pre-branch PlanConfig defaults, taken from
# `git show 9c81c41:ppc_engine/config.py`. Note makespan was ALWAYS 0.1
# on this side; 40 was the engine-side (classic) weight and never applied here.
# ---------------------------------------------------------------------------
_OLD_SEVERITY_TOL = 2.0
_OLD_SEVERITY_WEIGHT = 2.0
_OLD_SEVERITY_CAP = 60.0
_OLD_FAIRNESS_WEIGHT = 30.0
_OLD_MAKESPAN_WEIGHT = 0.1


def _old_severity(metrics):
    """The pre-branch _severity: squared lateness beyond a tolerance, capped.
    One-sided — early orders contributed nothing, which is the whole reason the
    objective was rewritten."""
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = late - _OLD_SEVERITY_TOL
        if over <= 0.0:
            continue
        if over > _OLD_SEVERITY_CAP:
            over = _OLD_SEVERITY_CAP
        total += over * over
    return total


def _old_score(metrics, config):
    """The pre-2026-08-06 ppc objective, reconstructed verbatim."""
    return (
        metrics.total_tardiness_days
        + _OLD_SEVERITY_WEIGHT * _old_severity(metrics)
        + config.ceiling_weight * _ceiling_breach(metrics, config)
        + config.committed_promise_weight * _committed_promise_breach(metrics, config)
        + _OLD_FAIRNESS_WEIGHT * metrics.max_tardiness_days
        + _OLD_MAKESPAN_WEIGHT * metrics.makespan_days
    )


_NEW_SCORE = _search.score          # search.py bound `score` at import time


def _use_old_objective():
    _search.score = _old_score


def _use_new_objective():
    _search.score = _NEW_SCORE


def _self_check():
    """Prove the swap reaches the search AND that the two formulas actually differ.

    An earlier version of this harness tried to reach the old objective by zeroing
    a weight. That silently produced pure makespan minimisation, and every number
    it printed would have been a comparison against a configuration that never ran.
    """
    _use_old_objective()
    assert _search.score is _old_score, "OLD swap did not reach the search"
    _use_new_objective()
    assert _search.score is _NEW_SCORE, "NEW swap did not reach the search"

    from ppc_engine.objective.metrics import PlanMetrics
    pm = PlanMetrics(total_tardiness_days=100.0, max_tardiness_days=30.0,
                     late_order_count=5, makespan_days=50.0,
                     lateness_by_order={("A", "x"): 30.0, ("B", "y"): -30.0},
                     promise_slip_by_order={})
    cfg = new_engine._plan_config(Config(plan_start_date=date(2026, 8, 6)))
    old_v, new_v = _old_score(pm, cfg), _NEW_SCORE(pm, cfg)
    assert old_v != new_v, (
        f"the two objectives score identically ({old_v}) — the comparison is meaningless")
    print(f"self-check OK: swap reaches the search; OLD={old_v:.1f} NEW={new_v:.1f} "
          f"on the same metrics\n")


def _outcomes(so_lines, masters, cfg, budget, seed):
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=budget, seed=seed)
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, cfg, masters, priority_rank=res.ranks)
    m = optimizer.plan_metrics(pr.schedule, so_lines, cfg.plan_start_date)

    due = {(l.so_no, l.item_code): l.delivery_date for l in so_lines}
    expected = {}
    for e in pr.schedule:
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            d = e.end.date()
            if k not in expected or d > expected[k]:
                expected[k] = d
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    return {
        "on_time": sum(1 for g in gaps if abs(g) <= BAND),
        "late_beyond": sum(1 for g in gaps if g > BAND),
        "early_beyond": sum(1 for g in gaps if g < -BAND),
        "worst": max((g for g in gaps if g > 0), default=0),
        "total_late": m["total_late_days"],
        "makespan": m["makespan_days"],
        "orders": len(gaps),
    }


def _baseline_ceiling(so_lines, masters, cfg, budget):
    """Derive the worst-order ceiling the same way production arms it.

    Production (`api/main.py:1282-1286`, inside `_start_optimize` — the single
    entry point for both the admin's manual "Start deep search" and the
    automatic daily run) reads `_incumbent_metrics().get("max_late_days")` —
    the CURRENTLY APPLIED plan's worst order — and feeds it through
    `worst_ceiling_days` into `PlanConfig.ceiling_days`
    (`engine/new_engine.py:205`) with `ceiling_weight=100`. This harness has
    no already-applied incumbent to read, so the faithful stand-in is one
    OLD-objective plan at seed 42 (the first of `SEEDS`), scored with
    `optimizer.plan_metrics` exactly the way `_incumbent_metrics()` does
    downstream. An earlier run of this harness left the ceiling unarmed
    entirely, which understated how dominant that term is against the new
    objective's smaller scale and made its verdict meaningless.
    """
    _use_old_objective()
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=budget, seed=42)
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, cfg, masters, priority_rank=res.ranks)
    m = optimizer.plan_metrics(pr.schedule, so_lines, cfg.plan_start_date)
    return float(m["max_late_days"])


def _best_of_seeds(so_lines, masters, cfg, budget, label):
    runs = [_outcomes(so_lines, masters, cfg, budget, s) for s in SEEDS]
    for s, r in zip(SEEDS, runs):
        print(f"    seed {s:5d}: on-time {r['on_time']:3d}  worst {r['worst']:3d}  "
              f"late>{BAND} {r['late_beyond']:3d}  early>{BAND} {r['early_beyond']:3d}")
    # "best" = most on time, worst order breaking ties
    best = max(runs, key=lambda r: (r["on_time"], -r["worst"]))
    print(f"  {label:22s} BEST-OF-3: on-time {best['on_time']}/{best['orders']}  "
          f"worst {best['worst']}d  late>{BAND} {best['late_beyond']}  "
          f"early>{BAND} {best['early_beyond']}  (total late-days {best['total_late']}, "
          f"makespan {best['makespan']:.1f})\n")
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--start", default="2026-08-06")
    args = ap.parse_args()

    with open(args.workbook, "rb") as fh:
        new_engine.set_masters_bytes(fh.read())
    so_lines, masters = loaders.load_all(args.workbook)
    # Settings pinned to LIVE production (verified against a plan pulled from the
    # running site 2026-08-06): overlap 84, consolidation window 10, flexible
    # machines OFF. The gate must measure at the settings the floor actually uses,
    # or its verdict does not generalise.
    cfg = Config(plan_start_date=date.fromisoformat(args.start), scheduler="new",
                 overlap_percent=84, flexible_machines=False,
                 apply_operator_logic=True, consolidation_window_days=10)
    _self_check()

    # Arm the worst-order ceiling exactly as production does on every real
    # optimize run (api/main.py:1282-1286, inside `_start_optimize`). Leaving
    # it unarmed (as an earlier run of this harness did) is not the
    # production configuration and its verdict means nothing. The SAME
    # ceiling value is used for both the OLD and NEW measurement runs below,
    # or the comparison is not controlled.
    ceiling = _baseline_ceiling(so_lines, masters, cfg, args.budget)
    cfg = replace(cfg, worst_ceiling_days=ceiling)
    print(f"worst-order ceiling armed at {ceiling:.1f} days (from an OLD-objective "
          f"seed-42 baseline plan, mirroring api/main.py:1282) — applied to BOTH "
          f"OLD and NEW runs below")
    print(f"{len(so_lines)} SO lines | budget {args.budget} | seeds {SEEDS}\n")

    print("OLD objective (pre-2026-08-06, reconstructed verbatim)")
    _use_old_objective()
    old = _best_of_seeds(so_lines, masters, cfg, args.budget, "OLD")

    print("NEW objective (symmetric on-time, makespan 0.1 tie-break)")
    _use_new_objective()
    new = _best_of_seeds(so_lines, masters, cfg, args.budget, "NEW")

    print("=" * 70)
    print("SHIP GATE")
    print("=" * 70)
    on_time_ok = new["on_time"] >= old["on_time"]
    worst_ok = new["worst"] <= old["worst"]
    improved = new["on_time"] > old["on_time"] or new["worst"] < old["worst"]
    print(f"  orders inside +/-{BAND} days : {old['on_time']:3d} -> {new['on_time']:3d}   "
          f"{'OK' if on_time_ok else 'FAIL (fell)'}")
    print(f"  worst single order        : {old['worst']:3d} -> {new['worst']:3d}   "
          f"{'OK' if worst_ok else 'FAIL (worse)'}")
    print(f"  at least one improved     : {'OK' if improved else 'FAIL (no change)'}")
    print(f"\n  VERDICT: {'SHIP' if (on_time_ok and worst_ok and improved) else 'DO NOT SHIP'}")
    print(f"\n  (reported, not gated: late>{BAND} {old['late_beyond']} -> {new['late_beyond']}, "
          f"early>{BAND} {old['early_beyond']} -> {new['early_beyond']}, "
          f"makespan {old['makespan']:.1f} -> {new['makespan']:.1f})")


if __name__ == "__main__":
    main()
