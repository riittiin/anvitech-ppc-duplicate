"""LINEAR vs SQUARED on-time term, measured on loaded books (2026-08-13).

    .venv/bin/python scripts/measure_linear_ontime.py --budget 250

WHY THIS EXISTS. The owner's rule of 2026-08-06 was that misses must be SPREAD
across orders, and squaring the overage is what delivered the spreading. The
measured consequence is that the search then REFUSES a plan that cuts total late
days by concentrating them — a CP solver found one 86 late-days better than the
live plan and the optimizer correctly rejected it. Correlation between the score
and total late-days, on a properly loaded book: squared -0.61, linear +0.49.
This harness is the A/B the owner asked for before deciding.

THE LOADING PROFILE IS THE WHOLE BALLGAME. ``tests/scaled_workbook`` as shipped
plans almost everything on time (seed 11: 0 late orders, 58 of 68 inside the
+/-4 band), so the on-time term barely fires and BOTH shapes score nearly
everything at zero. Three experiments in this project have already been
invalidated that way. ``--pull``/``--jitter`` move every SO delivery date earlier
by ``pull`` days plus a seeded per-order jitter, which is what turns a book with
nothing at stake into one shaped like the owner's. The default 8 +/- 28 was
chosen by sweeping both knobs until the LATE share of ``ontime_breach`` sat at
the owner's real ~85% (measured: 89.0 / 89.2 / 80.8 on seeds 11 / 23 / 5).
The profile is printed with every run — read it before reading the table.

WHAT IS AND IS NOT CONTROLLED. The two arms differ in EXACTLY one expression:
``roster_engine.objective.score``'s breach accumulator, ``over`` vs ``over*over``.
Everything else — band, cap, abs(), weights, the makespan tie-break, the seeds,
the budget, the book — is identical, and ``_self_check`` proves the swap really
reaches the search and that the two formulas really differ. The worst-order
CEILING is left DORMANT (``worst_ceiling_days=None``). Production arms it from
the incumbent plan on every optimize run, but there is no incumbent here, and a
squared 100-weight barrier would swamp exactly the term under test — so this
measures the on-time shape in isolation, which is the question being asked.

NOT THE OWNER'S BOOK. ``Test5.xlsx`` and friends are gitignored. Every number is
a comparison of two objectives on the SAME synthetic book, which is what makes
it fair; none of them is a forecast for Anvitech.
"""
import argparse
import dataclasses
import io
import random
import sys
from datetime import timedelta

sys.path.insert(0, ".")

from engine.config import Config
from engine.loaders import load_all
from engine.models import PlanRun
from engine.optimizer import ONTIME_BAND_DAYS as BAND
from engine.optimizer import ONTIME_CAP_DAYS as CAP
from engine import optimizer
from engine.optimizer import expected_completion, plan_metrics
from engine.pipeline import run_forward
from engine.rules import rule1_consolidate
from roster_engine import objective
from roster_engine import report as rreport
from tests.scaled_workbook import PLAN_START, build_scaled_bytes

# The four guarantees the roster engine exists to make. They must be 0 under
# BOTH objectives: a scoring change may buy late days, never a broken plan.
KINDS = ("OPERATOR_SPLIT_SHIFT", "OPERATION_SEGMENTED", "MACHINE_DOUBLE_BOOKED",
         "IDLE_CAPACITY")

_LINEAR = objective.score          # the working tree's shape, bound at import


def _squared_score(metrics, config) -> float:
    """The objective EXACTLY as it stood before this change.

    Reconstructed rather than imported, for the same reason
    ``scripts/measure_ontime.py`` reconstructs its baseline: the old shape no
    longer exists in the tree, and recovering it by zeroing a weight would
    measure against a configuration that never ran. Term order and the ceiling
    delegation match the live function verbatim.
    """
    band = objective._knob(config, "ontime_band_days", objective._BAND_DAYS)
    cap = objective._knob(config, "ontime_cap_days", objective._CAP_DAYS)
    breach = 0.0
    for late in metrics.lateness_by_order.values():
        over = abs(late) - band
        if over > 0:
            if over > cap:
                over = cap
            breach += over * over                      # <-- the only difference
    return (objective._knob(config, "ontime_weight", objective._ONTIME_WEIGHT) * breach
            + objective._knob(config, "ceiling_weight", objective._CEILING_WEIGHT)
            * objective._ceiling_breach(metrics, config)
            + objective._knob(config, "makespan_weight", objective._MAKESPAN_WEIGHT)
            * metrics.makespan_days)


def _use(shape):
    objective.score = _squared_score if shape == "squared" else _LINEAR


def _self_check(cfg):
    """Prove the swap reaches the SEARCH, and that the two shapes really differ.

    ``roster_engine.search`` calls ``objective.score(...)`` through the module,
    so rebinding the attribute reaches it — but that is an assumption worth
    testing rather than believing, and an earlier harness in this repo shipped
    numbers measured against a swap that never landed.
    """
    from roster_engine import search as roster_search
    for shape, fn in (("squared", _squared_score), ("linear", _LINEAR)):
        _use(shape)
        assert roster_search.objective.score is fn, f"{shape} swap did not reach the search"
    m = objective.Metrics({"A": 30.0, "B": -30.0, "C": 6.0}, 50.0, 30.0, 30.0)
    sq, li = _squared_score(m, cfg), _LINEAR(m, cfg)
    assert sq != li, f"the two objectives score identically ({sq}) — comparison is meaningless"
    print(f"self-check OK: swap reaches the search; SQUARED={sq:.1f} LINEAR={li:.1f} "
          f"on the same metrics")


def load_book(seed, n_items, n_orders, pull, jitter):
    """The scaled book with every delivery date pulled earlier — see the module
    docstring on why the shipped dates cannot answer this question."""
    raw = build_scaled_bytes(n_items=n_items, n_orders=n_orders, seed=seed)
    so_lines, masters = load_all(io.BytesIO(raw))
    rng = random.Random(seed * 31 + 5)
    so_lines = [dataclasses.replace(
        line, delivery_date=line.delivery_date - timedelta(
            days=pull + rng.randint(-jitter, jitter)))
        for line in so_lines]
    return so_lines, masters


def config(plan_start=PLAN_START):
    return Config(plan_start_date=plan_start, scheduler="roster",
                  overlap_percent=80, setup_time_min=90.0,
                  apply_operator_logic=True, consolidation_window_days=10)


def _gaps(schedule, so_lines):
    due = {(l.so_no, l.item_code): l.delivery_date for l in so_lines}
    exp = expected_completion(schedule)
    return [(exp[k] - due[k]).days for k in exp if k in due]


def profile(so_lines, masters, cfg):
    """The LOADING PROFILE: what share of the on-time breach is LATENESS.

    Measured on the unsearched (Rule 1-3 order) plan, so it describes the BOOK
    rather than either objective's answer to it. A book whose breach is mostly
    earliness cannot tell two lateness-shaped objectives apart.
    """
    run = PlanRun(so_lines=list(so_lines))
    run_forward(run, cfg, masters)
    gaps = _gaps(run.schedule, so_lines)
    late_b = early_b = 0.0
    for g in gaps:
        over = min(abs(g) - BAND, CAP)
        if over > 0:
            (late_b, early_b) = ((late_b + over, early_b) if g > 0
                                 else (late_b, early_b + over))
    total = late_b + early_b
    return {"orders": len(gaps),
            "late": sum(1 for g in gaps if g > BAND),
            "early": sum(1 for g in gaps if g < -BAND),
            "inside": sum(1 for g in gaps if abs(g) <= BAND),
            "late_share": (100.0 * late_b / total) if total else 0.0}


def measure(so_lines, masters, cfg, seed, budget):
    """Search, then REPLAY the winner and measure the plan the app would build."""
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=budget, seed=seed)
    # The winning CREW genome rides on the config, exactly as
    # ``roster_adapter.optimize_sequence`` replays it — replaying without it would
    # measure a different plan from the one the search actually picked.
    cfg = dataclasses.replace(cfg, crew_rank=dict(res.crew_rank or {}) or None)
    run = PlanRun(so_lines=list(so_lines))
    run_forward(run, cfg, masters, priority_rank=res.ranks)
    m = plan_metrics(run.schedule, so_lines, cfg.plan_start_date)
    batches = rule1_consolidate.run(list(so_lines), config=cfg, masters=masters)
    rows = rreport.all_violations(run.schedule, masters, cfg, batches=batches)
    counts = {k: sum(1 for r in rows if r["kind"] == k) for k in KINDS}
    gaps = _gaps(run.schedule, so_lines)
    return {"late_days": m["total_late_days"], "late_orders": m["late_orders"],
            "worst": m["max_late_days"], "makespan": m["makespan_days"],
            "inside": sum(1 for g in gaps if abs(g) <= BAND),
            "violations": counts,
            "bad": sum(counts.values())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=250)
    ap.add_argument("--seeds", type=int, nargs="+", default=[11, 23, 5])
    ap.add_argument("--pull", type=int, default=8)
    ap.add_argument("--jitter", type=int, default=28)
    ap.add_argument("--items", type=int, default=40)
    ap.add_argument("--orders", type=int, default=68)
    args = ap.parse_args()

    cfg = config()
    _self_check(cfg)
    print(f"book: scaled n_items={args.items} n_orders={args.orders} | dates pulled "
          f"{args.pull}d earlier +/- {args.jitter}d seeded jitter | budget {args.budget}\n")

    rows = []
    for seed in args.seeds:
        so_lines, masters = load_book(seed, args.items, args.orders,
                                      args.pull, args.jitter)
        prof = profile(so_lines, masters, cfg)
        print(f"seed {seed}: LOADING PROFILE — {prof['late']} late / {prof['early']} early / "
              f"{prof['inside']} inside band of {prof['orders']}; "
              f"LATE share of ontime_breach {prof['late_share']:.1f}%")
        for shape in ("squared", "linear"):
            _use(shape)
            out = measure(so_lines, masters, cfg, seed, args.budget)
            rows.append((seed, shape, out))
            print(f"   {shape:8s} late-days {out['late_days']:6d}  late orders {out['late_orders']:3d}"
                  f"  worst {out['worst']:3d}d  makespan {out['makespan']:6.2f}  "
                  f"inside-band {out['inside']:3d}  violations {out['bad']} {out['violations']}")
        print()

    print("=" * 78)
    print(f"{'seed':>5} {'on-time term':<14} {'late-days':>10} {'late orders':>12} "
          f"{'worst (d)':>10} {'makespan':>9} {'viol':>5}")
    print("-" * 78)
    for seed, shape, o in rows:
        print(f"{seed:>5} {shape:<14} {o['late_days']:>10} {o['late_orders']:>12} "
              f"{o['worst']:>10} {o['makespan']:>9.2f} {o['bad']:>5}")


if __name__ == "__main__":
    main()
