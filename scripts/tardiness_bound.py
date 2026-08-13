"""How good could the plan POSSIBLY be? — a certified lower bound on late-days.

READ-ONLY DIAGNOSTIC. Changes nothing, plans nothing, writes nothing. It exists to
answer one question the search cannot: when the optimizer says "no better job order
found", is that because the plan is near-optimal, or because a greedy dispatcher plus
a local search cannot see the better plan?

The engine is a heuristic. It reports how good a plan is RELATIVE to other plans it
tried. It has no idea how far it sits from the best plan that exists. This builds a
constraint-programming model of the same book and asks a solver (OR-Tools CP-SAT, via
PyJobShop) for a proven floor: **no schedule of this book can beat N late-days.**

    bound 380 against a live plan of 406  ->  within 7%. Stop optimising; the four
                                              20+ day orders need outsourcing, a
                                              shift, or a new date.
    bound 200 against a live plan of 406  ->  half the loss is reachable and the
                                              engine cannot see it. Worth real work.

WHY A RELAXATION IS STILL A VALID ANSWER
----------------------------------------
The model deliberately DROPS one of the shop's four rules: Rule 1, one operator mans
one machine for a whole shift. PyJobShop cannot express "this person may not be on a
different machine within the same shift" — that needs raw CP-SAT booleans per
(operator, machine, shift).

Dropping a constraint can only make the problem EASIER, so the optimum of this model
is less than or equal to the true optimum under all four rules:

    bound(relaxed)  <=  bound(all four rules)  <=  the best real schedule  <=  406

So the number this prints is a floor on the floor. If it comes back close to 406,
that is conclusive — no amount of engine work can help. If it comes back far below,
the answer is "maybe", not "definitely", because part of the gap may be Rule 1 itself
rather than the search. That asymmetry is the whole reason the tool is worth running:
the CHEAP direction is the one that ends the investigation.

Everything else IS modelled, exactly as the engine does it:
  * machine calendars, shifts, weekly off and holidays  -> per-machine breaks
  * an operation runs to completion, never interrupted  -> free; a CP interval is
    contiguous by construction (that is Rule 2)
  * an operation may span a night with the part in the chuck -> allow_breaks=True
  * whole-piece overlap release                         -> start-before-start delay
  * a step never finishes before the step feeding it    -> end-before-end
  * OS / DISPATCH fully sequential both sides           -> end-before-start
  * 90-min setup only on a real (item, process) change  -> sequence-dependent setup
  * operators cannot be in two places at once (level 2) -> capacity-1 renewables

Usage
-----
    pip install pyjobshop
    python scripts/tardiness_bound.py --level operators --time-limit 900

Reads the SAME book the app plans — same store, same Rule 1 consolidation — so the
number is comparable to what the Schedule tab shows. Needs the store env the app
uses (MONGODB_URI, or the local file store). It only ever reads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MINUTES_PER_DAY = 1440


# --------------------------------------------------------------------------- #
# The book — loaded exactly the way the app loads it
# --------------------------------------------------------------------------- #

def load_demo_book():
    """The repo's own generated workbook — exercises the whole model path without
    needing the store. For proving the tool runs, never for a real answer."""
    import io

    from engine.config import Config
    from engine.loaders import load_all
    from engine.rules import rule1_consolidate
    from tests.scaled_workbook import PLAN_START, build_scaled_bytes

    so_lines, masters = load_all(io.BytesIO(build_scaled_bytes(seed=7)))
    config = Config(plan_start_date=PLAN_START, scheduler="roster",
                    overlap_percent=80, setup_time_min=90.0,
                    apply_operator_logic=True)
    plan_start = dt.datetime.combine(PLAN_START,
                                     dt.time(config.first_shift_start_hour, 0))
    return (rule1_consolidate.run(list(so_lines), config=config, masters=masters),
            masters, config, plan_start)


def load_book():
    """(batches, masters, config, plan_start) — the same objects the engine plans.

    Goes through the app's own order book and Rule 1, so the batches here are the
    batches the scheduler sees, clubbed the same way. Anything else would be
    measuring a different problem.
    """
    import dataclasses
    import io
    import json

    from engine import book_store, operator_master, orderbook
    from engine.config import Config
    from engine.loaders import load_all
    from engine.rules import rule1_consolidate

    raw_xlsx = book_store.load_masters_bytes()
    if not raw_xlsx:
        raise SystemExit("no masters workbook in the store — upload one first")
    _report, masters = load_all(io.BytesIO(raw_xlsx))
    if not masters.routings:
        raise SystemExit("the stored workbook has no routings")

    # Operators are APP-OWNED: the Settings table, never the workbook's operator
    # sheet (which is a fossil after the one-time seed). api._current_masters does
    # exactly this overlay on every call, so the bound must too — otherwise it
    # would be computed against a crew the shop does not have.
    table = book_store.load_operator_table()
    if table:
        masters = dataclasses.replace(
            masters, operators=operator_master.operators_as_of(table, dt.date.today()))

    orders = book_store.load_active_orders()
    actuals = book_store.load_actuals()
    so_lines = orderbook.active_so_lines(orders, actuals, masters)
    if not so_lines:
        import os
        have_up = bool(os.environ.get("UPSTREAM_MONGODB_URI"))
        raise SystemExit(
            "The store has a masters workbook but NO active orders.\n"
            f"  MONGODB_URI set          : {bool(os.environ.get('MONGODB_URI'))}\n"
            f"  UPSTREAM_MONGODB_URI set : {have_up}\n"
            + ("" if have_up else
               "\nThe duplicate mirrors the live book through an OverlayStore, so the\n"
               "ORDERS live on the upstream (live) cluster, not the duplicate's own.\n"
               "Add UPSTREAM_MONGODB_URI as a repo secret — the same read-only value\n"
               "the duplicate's Render service uses — and run again."))

    raw_cfg = book_store.load_plan_config()
    config = Config.from_dict(json.loads(raw_cfg)) if raw_cfg else Config()
    plan_start_date = config.plan_start_date or dt.date.today()
    plan_start = dt.datetime.combine(
        plan_start_date, dt.time(config.first_shift_start_hour, 0))

    batches = rule1_consolidate.run(list(so_lines), config=config, masters=masters)
    return batches, masters, config, plan_start


# --------------------------------------------------------------------------- #
# Calendars — a machine's NON-working minutes become CP "breaks"
# --------------------------------------------------------------------------- #

def machine_breaks(machine, masters, config, plan_start, horizon_min):
    """Every interval this machine cannot work, in minutes from plan_start.

    Mirrors roster_engine.worktime: a two-shift machine runs 08:00-05:00 (next day),
    a single-shift one runs the FIRST shift only. Non-working days are closed
    entirely. Getting this wrong in either direction breaks the bound — too few
    breaks and the floor is below anything achievable, too many and it is not a
    floor at all.
    """
    first_a = config.first_shift_start_hour
    first_b = config.first_shift_end_hour
    second_b = config.second_shift_end_hour          # crosses midnight
    two_shift = bool(machine.is_two_shift())

    breaks, day = [], plan_start.date()
    end_date = (plan_start + dt.timedelta(minutes=horizon_min)).date()
    while day <= end_date:
        base = (dt.datetime.combine(day, dt.time(0, 0)) - plan_start
                ).total_seconds() / 60.0
        if not masters.calendar.is_working_day(day):
            breaks.append((base, base + MINUTES_PER_DAY))       # closed all day
        elif two_shift:
            # works 08:00 -> 05:00 next day; idle 05:00 -> 08:00
            breaks.append((base + second_b * 60, base + first_a * 60))
        else:
            # first shift only: idle 00:00 -> 08:00 and 19:00 -> 24:00
            breaks.append((base, base + first_a * 60))
            breaks.append((base + first_b * 60, base + MINUTES_PER_DAY))
        day += dt.timedelta(days=1)

    out = []
    for s, e in breaks:
        s, e = max(0, int(s)), min(int(horizon_min), int(e))
        if e > s:
            out.append((s, e))
    return out


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #

def build(batches, masters, config, plan_start, level, horizon_days):
    from pyjobshop import Model

    from roster_engine.domain import (DISPATCH, MACHINING, OUTSOURCED,
                                      build_jobs, is_machining_machine)

    horizon_min = horizon_days * MINUTES_PER_DAY
    jobs, _by_key, skipped = build_jobs(
        batches, masters, bool(getattr(config, "flexible_machines", False)))
    if skipped:
        print(f"  skipped {len(skipped)} item(s) with no routing: "
              f"{', '.join(sorted(skipped)[:5])}")

    m = Model()

    # --- resources ---------------------------------------------------------- #
    mach = {}
    for mid, machine in sorted(masters.machines.items()):
        mach[mid] = m.add_machine(
            breaks=machine_breaks(machine, masters, config, plan_start, horizon_min),
            name=mid)

    # Outsourcing: a flat 24x7 block, unlimited parallel, no operator and no
    # calendar — exactly how the engine treats an OS step. Modelled as one
    # renewable with capacity high enough that it never binds.
    os_pool = m.add_renewable(capacity=max(1, len(batches) + 1), name="OS")

    ops_res = {}
    if level != "machines":
        # An operator is a capacity-1 renewable: they cannot be in two places at
        # once. This is the part of Rule 1 that CP can express. The rest of it —
        # locked to ONE machine for a whole shift — is the relaxation, documented
        # in the module docstring.
        for o in sorted(masters.operators, key=lambda o: o.name):
            ops_res[o.name] = m.add_renewable(capacity=1, name=o.name)

    qualified = {}
    for o in masters.operators:
        for mid in (getattr(o, "machines", None) or ()):
            qualified.setdefault(mid, []).append(o.name)

    # --- tasks -------------------------------------------------------------- #
    setup_min = int(getattr(config, "setup_time_min", 90) or 0)
    # (task, setup_key) pairs per machine. PyJobShop's Task is not hashable, so the
    # setup key travels alongside the task rather than in a dict keyed by it.
    per_machine = {mid: [] for mid in mach}
    n_modes = 0
    unstaffed = set()

    for job in jobs:
        due = job.due
        # An order finishing any time ON its delivery date is on time, matching the
        # app's `(completion_date - due_date).days <= 0`.
        due_min = (int((dt.datetime.combine(due, dt.time(0, 0)) - plan_start
                        ).total_seconds() / 60) + MINUTES_PER_DAY) if due else None
        cp_job = m.add_job(due_date=due_min, name=job.key)

        prev = prev_op = None
        for op in job.ops:
            qty = job.qty_for(op.seq)
            if op.kind == DISPATCH or (qty <= 0 and not op.machine_options):
                continue

            if op.kind == OUTSOURCED:
                t = m.add_task(job=cp_job, allow_breaks=True,
                               name=f"{job.key}/{op.seq}")
                m.add_mode(t, os_pool, int(max(1, op.cycle_min)), demands=1)
            else:
                options = [mid for mid in op.machine_options if mid in mach]
                if not options:
                    continue
                # Cutting only. The 90-min setup is added BETWEEN consecutive tasks
                # via add_setup_time, so it is charged exactly when the machine
                # really changes part or side — never on a repeat.
                dur = max(1, int(round(qty * op.cycle_min)))
                t = m.add_task(job=cp_job, allow_breaks=True,
                               name=f"{job.key}/{op.seq}")
                added = 0
                for mid in options:
                    if level != "machines":
                        for name in qualified.get(mid, ()):
                            if name in ops_res:
                                # A Machine is UNARY and takes no capacity demand:
                                # 0 for the machine, 1 for the operator. [1, 1] is
                                # rejected as "infeasible demands".
                                m.add_mode(t, [mach[mid], ops_res[name]], dur,
                                           demands=[0, 1])
                                added += 1
                if added == 0:
                    # Either level 'machines', or NO operator in the Settings table
                    # is qualified for any of this step's machines — which leaves
                    # the task with zero modes and makes the model invalid. Falling
                    # back to a machine-only mode assumes someone could run it: a
                    # relaxation, so the result stays a valid FLOOR. The app reports
                    # the same gap separately as MACHINE_NO_OPERATOR.
                    for mid in options:
                        m.add_mode(t, mach[mid], dur)
                        added += 1
                    if level != "machines":
                        unstaffed.add("/".join(options))
                n_modes += added
                for mid in options:
                    per_machine[mid].append((t, (job.item_code, op.seq)))

            if prev is not None:
                if prev_op.kind == OUTSOURCED or op.kind == OUTSOURCED:
                    m.add_end_before_start(prev, t)          # OS: sequential both sides
                else:
                    # Rule 5, in whole pieces: the successor may start once
                    # ceil(overlap x qty) pieces have cleared its predecessor.
                    m.add_start_before_start(prev, t, delay=_release_delay(
                        job, prev_op, config, setup_min))
                    # ...and it may never FINISH before the step feeding it.
                    m.add_end_before_end(prev, t)
            prev, prev_op = t, op

    # --- 90-minute setup, only on a real (item, process) change -------------- #
    n_setup = 0
    for mid, tasks in per_machine.items():
        machine = masters.machines.get(mid)
        if machine is None or not is_machining_machine(machine):
            continue
        for a, ka in tasks:
            for b, kb in tasks:
                if a is not b and ka != kb:
                    m.add_setup_time(mach[mid], a, b, setup_min)
                    n_setup += 1

    if unstaffed:
        print(f"  {len(unstaffed)} machine group(s) have NO qualified operator in "
              f"Settings — modelled machine-only (a relaxation): "
              f"{', '.join(sorted(unstaffed)[:4])}")
    m.set_objective(weight_total_tardiness=1)
    print(f"  model: {len(jobs)} jobs, {len(mach)} machines, "
          f"{len(ops_res)} operators, {n_modes} modes, {n_setup} setup pairs")
    return m


def _release_delay(job, op, config, setup_min):
    """Worked minutes on ``op`` before its successor may start — the same whole-piece
    rule as roster_engine.release, expressed as a start-to-start delay."""
    import math

    from roster_engine.domain import MACHINING
    qty = job.qty_for(op.seq)
    if qty <= 0 or op.cycle_min <= 0:
        return 0
    setup = setup_min if op.kind == MACHINING else 0
    overlap = min(1.0, max(0.0, float(getattr(config, "overlap_percent", 100)) / 100.0))
    pieces = max(1, min(qty, math.ceil(overlap * qty)))
    return int(setup + pieces * op.cycle_min)


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--level", choices=["machines", "operators"], default="operators",
                    help="machines: machines only (loosest, fastest). "
                         "operators: + nobody in two places at once (tighter).")
    ap.add_argument("--time-limit", type=float, default=600.0, help="seconds")
    ap.add_argument("--horizon-days", type=int, default=120)
    ap.add_argument("--workers", type=int, default=0, help="0 = all cores")
    ap.add_argument("--demo", action="store_true",
                    help="use the repo's generated book instead of the store — "
                         "proves the tool runs; not a real answer")
    args = ap.parse_args()

    try:
        import pyjobshop  # noqa: F401
    except ImportError:
        raise SystemExit("pyjobshop is not installed.  pip install pyjobshop")

    if args.demo:
        print("DEMO book (repo fixture) — this is a self-test, not your shop.")
        batches, masters, config, plan_start = load_demo_book()
    else:
        print("Loading the book from the store (read-only)...")
        batches, masters, config, plan_start = load_book()
    print(f"  {len(batches)} batches, plan start {plan_start:%d-%m-%Y %H:%M}, "
          f"overlap {getattr(config, 'overlap_percent', '?')}%")

    print(f"Building the CP model at level '{args.level}'...")
    model = build(batches, masters, config, plan_start, args.level, args.horizon_days)

    print(f"Solving (limit {args.time_limit:.0f}s)...")
    res = model.solve(time_limit=args.time_limit, display=True,
                      num_workers=args.workers or None)

    lb_min = getattr(res, "lower_bound", None)
    obj_min = getattr(res, "objective", None)
    print("\n" + "=" * 68)
    print(f"status               : {getattr(res, 'status', '?')}")
    if obj_min is not None:
        print(f"best found           : {obj_min / MINUTES_PER_DAY:>9.1f} late-days")
    if lb_min is not None:
        print(f"PROVEN FLOOR         : {lb_min / MINUTES_PER_DAY:>9.1f} late-days")
    print("=" * 68)
    print("""
HOW TO READ THIS
  Compare the floor against the late-days on your Schedule tab.

  floor close to the live number  -> the plan is near the best that exists. No
      engine work will help; the remaining lateness needs outsourcing, another
      shift, or renegotiated dates.

  floor far below it              -> some of the gap is reachable. Part may be
      Rule 1 (one operator, one machine, one shift), which this model DROPS and
      which genuinely costs capacity — so treat it as "worth investigating",
      not as "the engine is leaving that much on the table".

  The floor is valid either way: dropping a constraint can only make the problem
  easier, so the true optimum under all four rules is at or above this number.""")


if __name__ == "__main__":
    main()
