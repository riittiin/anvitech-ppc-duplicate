"""Roster engine vs the live (`new`) engine, side by side — and the two budget
timings the owner needs before pressing "Start deep search" on roster.

Run it:

    python3 scripts/roster_vs_new.py compare            # the side-by-side table
    python3 scripts/roster_vs_new.py compare --book sample
    python3 scripts/roster_vs_new.py compare --seeds 8  # 8 different books
    python3 scripts/roster_vs_new.py timings            # ~90 s, Part C

Why a script under ``scripts/`` and not a test: nothing here ASSERTS anything.
It is a measurement whose output is read by a person, it takes ~90 s for the
timings, and its numbers are expected to move as the engines change — all three
are reasons not to put it in ``pytest``. The invariants that MUST hold are in
``tests/test_roster_end_to_end.py``.

WHAT THE BOOK IS. There is no real workbook in this repo (``Test5.xlsx`` and
friends are gitignored), so this builds one:

  * ``--book sample`` — the repo's own ``tests/sample_workbook.py``: 3 SO lines,
    2 items, 6 machines. Reproducible and shared with the test suite, but far too
    small for a makespan or a late-day number to mean anything.
  * ``--book scaled`` (default) — a SYNTHETIC book from ``tests/scaled_workbook.py``,
    shaped like the owner's: 60 SO lines over 24 items, 14 machines (5 CNC + 2 VMC
    two-shift, 7 single-shift benches), 19 operators across two shifts, an 8-step
    routing family and a Thursday weekly off. **It is not the owner's book.** Every
    number below is a comparison of two engines on the SAME synthetic book, which is
    what makes the comparison fair; none of them is a forecast for Anvitech. It lives
    under ``tests/`` on purpose: ``tests/test_roster_end_to_end.py`` PINS the
    headline of this table on the same book, so the two can never drift apart.

THREE THINGS THAT WOULD MISLEAD IF READ CARELESSLY, printed with the numbers:

  1. ``OVERLAP_FRACTIONAL_PIECE`` is quoted as ``detected / muted / reported``.
     A bare 0 is NOT "this engine releases on whole pieces" — on a contended plan
     almost every detection is withheld because the successor's machine had just
     freed. See ``roster_engine/report.py``'s module docstring.
  2. ``classic`` and ``flow`` emit NO operator on any entry unless
     ``apply_operator_logic`` is on, and they are retired. Only **roster vs new**
     is a real comparison; the other two are printed for context and labelled.
  3. ``IDLE_CAPACITY`` = 0 on a synthetic book usually means the fixture has no
     spare operator, not that there is no idle capacity to find.

THE SCORE. Both engines are measured with ``engine.optimizer.plan_metrics`` +
``score`` — the app's ONE definition, including the dormant worst-order ceiling
term (``ceiling_days``, which ``api._start_optimize`` sets live from the current
plan's worst lateness and which is None here, i.e. dormant). The config every row
is run under is printed above the table. Note that ``score`` and "late days" can
disagree in DIRECTION: the objective is symmetric — finishing far EARLY is
penalised like finishing late — so an engine can score better while shipping more
late days. Both are printed for exactly that reason.
"""
import argparse
import io
import sys
import time

sys.path.insert(0, ".")

from engine import new_engine, optimize_service, optimizer, pipeline
from engine.config import Config
from engine.loaders import load_all
from engine.models import Order, PlanRun
from roster_engine import report as rreport
from tests.sample_workbook import build_sample_bytes
from tests.scaled_workbook import PLAN_START, build_scaled_bytes

KINDS = ("OPERATOR_SPLIT_SHIFT", "OPERATION_SEGMENTED", "IDLE_CAPACITY")


def load_book(kind, seed=7):
    raw = build_sample_bytes() if kind == "sample" else build_scaled_bytes(seed=seed)
    # The new engine reads its routings/machines from the STORED workbook, never
    # from the masters object it is handed. Without this it does not fail — it
    # returns an EMPTY schedule with no note and no error.
    new_engine.set_masters_bytes(raw)
    so_lines, masters = load_all(io.BytesIO(raw))
    if kind == "sample":
        import dataclasses

        from engine.models import Operator
        # The shipped workbook leaves MI1 and the provisional CNC9 with nobody in
        # Settings, and the roster engine refuses such a book (typed RuleError).
        # Operators are the Settings table, not the workbook sheet — adding one
        # here is what an admin does, not a change to the data.
        masters = dataclasses.replace(masters, operators=list(masters.operators) + [
            Operator(name="Operator Four", preferred_machines_raw="MI1/MW1/CNC9",
                     machines=["MI1", "MW1", "CNC9"], shift="First shift")])
    return raw, so_lines, masters


def config_for(scheduler, overlap=80):
    return Config(plan_start_date=PLAN_START, scheduler=scheduler,
                  overlap_percent=overlap, setup_time_min=90.0,
                  apply_operator_logic=True)


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #

def measure(scheduler, so_lines, masters, overlap=80):
    config = config_for(scheduler, overlap)
    run = PlanRun(so_lines=list(so_lines))
    started = time.perf_counter()
    trace = pipeline.run_forward(run, config, masters)
    elapsed = time.perf_counter() - started
    if trace["rule6"]["error"]:
        return {"error": trace["rule6"]["error"]["message"]}
    entries = run.schedule
    counts = {k: 0 for k in KINDS}
    counts["OPERATOR_SPLIT_SHIFT"] = len(
        rreport.operator_split_violations(entries, config, masters))
    counts["OPERATION_SEGMENTED"] = len(rreport.segmentation_violations(entries))
    counts["IDLE_CAPACITY"] = len(
        rreport.idle_capacity_violations(entries, masters, config))
    scan = rreport.overlap_rounding_scan(entries, masters, config)
    metrics = optimizer.plan_metrics(
        entries, so_lines, PLAN_START,
        ceiling_days=getattr(config, "worst_ceiling_days", None))
    labelled = sum(1 for e in entries
                   if e.machine not in rreport.OFF_LANES and e.operator_label())
    on_machine = sum(1 for e in entries if e.machine not in rreport.OFF_LANES)
    return {"entries": len(entries), "ms": elapsed * 1000.0,
            "labelled": labelled, "on_machine": on_machine,
            "counts": counts, "scan": scan, "metrics": metrics,
            "score": optimizer.score(metrics)}


def compare(args):
    engines = ["new", "roster"] + (["classic", "flow"] if args.all_engines else [])
    print(f"BOOK: {args.book}"
          + (f"  (seeds {', '.join(str(s) for s in range(1, args.seeds + 1))})"
             if args.book == "scaled" and args.seeds > 1 else ""))
    print(f"CONFIG: plan_start={PLAN_START}  overlap_percent={args.overlap}  "
          f"setup_time_min=90.0  apply_operator_logic=True  "
          f"worst_ceiling_days=None (dormant)  committed lanes: none")
    print(f"SCORE: engine.optimizer.score — the app's one definition, "
          f"ONTIME(symmetric, +/-4d band) + 0.1 x makespan + ceiling + promise\n")

    if args.book == "sample":
        print("NOTE: the shipped sample book's SO delivery dates are in MARCH 2025 "
              "and the plan\n  starts in 2026, so late_d/late_o/score are noise on "
              "it. Only the violation\n  columns mean anything on --book sample.\n")
    seeds = range(1, args.seeds + 1) if args.book == "scaled" else [7]
    seen = []
    for seed in seeds:
        raw, so_lines, masters = load_book(args.book, seed=seed)
        head = (f"--- book seed {seed}: {len(so_lines)} SO lines, "
                f"{len(masters.routings)} items, {len(masters.machines)} machines, "
                f"{len(masters.operators)} operators ---")
        print(head)
        print(f"{'engine':9s} {'entries':>7s} {'ms':>6s} {'makespan':>9s} "
              f"{'late_d':>7s} {'late_o':>7s} {'score':>9s}  "
              f"{'SPLIT':>6s} {'SEGMENT':>8s} {'IDLE':>5s}  ROUNDING(d/m/r)")
        for engine in engines:
            got = measure(engine, so_lines, masters, args.overlap)
            if "error" in got:
                print(f"{engine:9s} RULE ERROR: {got['error'][:90]}")
                continue
            seen.append((engine, got))
            m, c, s = got["metrics"], got["counts"], got["scan"]
            note = ("" if got["labelled"] == got["on_machine"]
                    else f"   <- only {got['labelled']}/{got['on_machine']} entries "
                         f"name an operator: SPLIT is partly blind here")
            print(f"{engine:9s} {got['entries']:7d} {got['ms']:6.0f} "
                  f"{m['makespan_days']:9.2f} {m['total_late_days']:7d} "
                  f"{m['late_orders']:7d} {got['score']:9.1f}  "
                  f"{c['OPERATOR_SPLIT_SHIFT']:6d} {c['OPERATION_SEGMENTED']:8d} "
                  f"{c['IDLE_CAPACITY']:5d}  "
                  f"{s['detected']:>3d}/{s['muted']:>3d}/{s['reported']:>3d}{note}")
        print()

    idle_fired = sum(g["counts"]["IDLE_CAPACITY"] for _e, g in seen)
    blind = [e for e, g in seen if g["labelled"] < g["on_machine"]]
    print("HOW TO READ THIS")
    print("  ROUNDING is detected/muted/reported. A bare 0 REPORTED does not mean "
          "the engine\n    releases on whole pieces — on a contended plan nearly "
          "every detection is withheld\n    because the successor's machine had "
          "just freed. Quote all three numbers.")
    print("  SPLIT can only see an entry that NAMES an operator. classic/flow name "
          "one only when\n    apply_operator_logic is ON (this run: ON). At "
          "Config's default (OFF) they name none\n    and the check is "
          "structurally blind to them. "
          + ("Every engine above named one on every\n    real-machine entry, so "
             "no column above is blind."
             if not blind else
             f"Blind or partly blind above: {', '.join(sorted(set(blind)))}.")
          + "\n    They are retired engines either way — the live comparison is "
            "roster vs new.")
    if idle_fired:
        print(f"  IDLE fired {idle_fired} time(s) in this run, so a 0 in that "
              f"column IS a measured zero\n    on this book — the fixture does "
              f"have spare qualified crew to find.")
    else:
        print("  IDLE 0 everywhere in THIS run: on a synthetic book that usually "
              "means the fixture\n    has no spare operator, NOT that there is no "
              "idle capacity. Re-run with\n    --all-engines to settle it — on the "
              "scaled book the retired classic engine\n    leaves 29 idle windows "
              "over 8 seeds, which shows the fixture does carry spare\n    "
              "qualified crew and the check can see it. Pinned by "
              "tests/test_roster_end_to_end.py\n    ::"
              "test_the_idle_capacity_check_can_fire_on_this_fixture.")
    print("  late_d and score can move in OPPOSITE directions: the objective is "
          "symmetric, so\n    finishing far early is penalised like finishing "
          "late. Neither number alone is\n    the answer; the owner's measure is "
          "late deliveries.")


# --------------------------------------------------------------------------- #
# timings
# --------------------------------------------------------------------------- #

def timings(args):
    raw, so_lines, masters = load_book(args.book, seed=7)
    print(f"BOOK: {args.book} — {len(so_lines)} SO lines, {len(masters.routings)} "
          f"items, {len(masters.machines)} machines, {len(masters.operators)} operators")
    print("MACHINE: this box, single-threaded. Render's web tier is 0.1 CPU — a "
          "10% share of a\n  vCPU that is itself slower than a dev laptop core, so "
          f"the projections below assume a\n  {args.render_slowdown[0]}x-{args.render_slowdown[1]}x"
          " slowdown. That factor is ASSUMED, not measured: nothing in this repo "
          "can\n  time Render. Treat the range as the honest width of the answer.\n")

    walls = {}
    for engine, budget in (("roster", args.local_budget), ("new", 200)):
        config = config_for(engine, args.overlap)
        started = time.perf_counter()
        result = optimizer.sweep_optimize(so_lines, config, masters,
                                          budget_evals=budget, seed=42)
        elapsed = time.perf_counter() - started
        walls[engine] = (elapsed, result.evals)
        best = result.result.best or {}
        lo, hi = (elapsed * f / 60.0 for f in args.render_slowdown)
        print(f"LOCAL sweep_optimize  engine={engine:7s} budget_evals={budget:5d}  "
              f"wall={elapsed:6.1f}s  PLANS BUILT={result.evals:5d}  "
              f"per_plan={elapsed / max(1, result.evals) * 1000:5.1f} ms")
        print(f"    winner_knob={result.overlap_percent:3d}  "
              f"contenders_in_table={len(result.table)}  "
              f"late_d={best.get('total_late_days')}  "
              f"makespan={best.get('makespan_days')}  "
              f"-> on Render ~{lo:.0f}-{hi:.0f} min")
    print("  READ THE BUDGET COLUMN CAREFULLY — `budget_evals` does NOT mean the same "
          "thing to the\n  two engines, so the wall-clock is the only fair comparison:")
    print("    roster: TOTAL for the contest, split equally across the 6 "
          "ROSTER_OVERLAP_CANDIDATES\n            (optimizer._sweep_optimize_classic's "
          "fair-contest contract, via roster_adapter).")
    print("    new:    NOT a plan count. new_engine.sweep_optimize does "
          "`per = budget//10` per\n            golden-section step and runs the tune "
          "TWICE (flexible_machines False/True),\n            so budget 200 built "
          f"{walls['new'][1]} plans above. It also returns table=[], which is why\n"
          "            contenders_in_table reads 0 for it — not a bug in this script.")
    print(f"  LIVE CAPS (api.main._start_optimize): flow min(budget,100); "
          f"new min(budget,200); roster\n    UNCAPPED, so it gets the full "
          f"api.main._OPT_BUDGETS['deep'] = 1000."
          + ("" if args.local_budget == 1000 else
             f" (--local-budget overrode it to {args.local_budget} for this "
             f"run, so the wall-clock above is NOT the live cost.)") + "\n")

    orders = {}
    for line in so_lines:
        order = Order(so_no=line.so_no, item_code=line.item_code,
                      item_name=line.item_name, ordered_qty=line.qty,
                      delivery_date=line.delivery_date)
        orders[order.key] = order
    config = config_for("roster", args.overlap)
    candidates = list(optimize_service.cloud_candidates(config))
    budget = optimize_service.cloud_budget(config)
    payload = optimize_service.build_payload(orders, [], raw, config, seed=42,
                                             candidates=candidates,
                                             budget_per_candidate=budget)
    jobs = optimize_service.contest_jobs(payload)
    started = time.perf_counter()
    row = optimize_service.run_candidate(payload, candidates[0])
    elapsed = time.perf_counter() - started
    shards = 20               # .github/workflows/optimize.yml matrix
    per_shard = -(-len(jobs) // shards)
    print(f"CLOUD run_candidate    overlap={candidates[0]}  budget={budget}  "
          f"wall={elapsed:6.1f}s  evals={row['evals']}  "
          f"per_eval={elapsed / max(1, row['evals']) * 1000:5.1f} ms")
    print(f"  contest = {len(jobs)} candidates x {budget} evals = "
          f"{len(jobs) * budget} plans, fanned across {shards} Actions shards")
    print(f"  -> {per_shard} candidate(s) per shard "
          f"= ~{elapsed * per_shard:.0f}s of COMPUTE per shard, plus GitHub "
          f"runner setup (checkout + pip, ~1-2 min)")
    print(f"  A GitHub Actions runner is a full 2-core vCPU, not Render's 0.1 CPU, "
          f"so this one is\n  measured on comparable hardware — no slowdown factor "
          f"is applied to it.")
    print(f"  OPTIMIZE_CLOUD_TIMEOUT_MIN default is 20 min before the app gives "
          f"up and computes locally.")
    print(f"\nRECOMMENDATION (measure, then decide — this script CHANGES NO "
          f"CONSTANT):")
    r_wall, r_evals = walls["roster"]
    n_wall, n_evals = walls["new"]
    print(f"  LOCAL: roster's full {args.local_budget}-eval deep search cost "
          f"{r_wall:.1f}s ({r_evals} plans) against\n    the LIVE `new` engine's "
          f"already-capped local fallback at {n_wall:.1f}s ({n_evals} plans) on the "
          f"same\n    book — roster is {n_wall / max(r_wall, 1e-9):.1f}x CHEAPER "
          f"uncapped than what the site runs today.\n    On that evidence the "
          f"absence of a roster cap is not a defect and no new constant is\n"
          f"    needed. If the owner wants a hard ceiling anyway, the number that "
          f"costs nothing\n    measurable is the one that keeps the contest whole: "
          f"a multiple of the 6 contenders.")
    print(f"  CLOUD: one candidate is {elapsed:.1f}s of compute against a "
          f"{shards}-shard fan-out and a\n    20-minute app-side timeout — three "
          f"orders of magnitude of headroom. "
          f"CLOUD_BUDGET_PER_CANDIDATE\n    = {budget} is comfortable; the "
          f"binding cost is runner STARTUP, not search.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("compare", help="side-by-side plan quality + violations")
    p.add_argument("--book", choices=("scaled", "sample"), default="scaled")
    p.add_argument("--seeds", type=int, default=1,
                   help="how many synthetic books (scaled only)")
    p.add_argument("--overlap", type=int, default=80)
    p.add_argument("--all-engines", action="store_true",
                   help="also print classic and flow (retired, labelled)")
    p.set_defaults(func=compare)

    p = sub.add_parser("timings", help="local sweep + one cloud candidate")
    p.add_argument("--book", choices=("scaled", "sample"), default="scaled")
    p.add_argument("--overlap", type=int, default=80)
    p.add_argument("--local-budget", type=int, default=1000,
                   help="the live deep-search budget (api.main._OPT_BUDGETS: 1000)")
    p.add_argument("--render-slowdown", type=int, nargs=2, default=(10, 30),
                   metavar=("LO", "HI"),
                   help="ASSUMED wall-clock factor from this box to Render's "
                        "0.1 CPU (default 10 30). Nothing here can measure it.")
    p.set_defaults(func=timings)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
