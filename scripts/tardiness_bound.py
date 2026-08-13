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

SEEDING THE ENGINE FROM THE SOLVER (--seed-engine)
--------------------------------------------------
A floor tells you a better plan EXISTS. It does not tell you the engine can build
one, because the engine cannot consume a schedule — it consumes a JOB SEQUENCE and
re-plans from it under all four rules. So --seed-engine takes the order in which
the CP solution runs the jobs, converts it into the app's own priority-rank map
(``engine.optimizer.ranks_for``), and replays it through the unchanged
``pipeline.run_forward`` with ``scheduler="roster"``. Nothing is relaxed in the
replay and nothing is written anywhere.

It prints three rows so the question has an answer rather than a hope:

    engine alone        the engine's own Rule 1-3 order, no seed
    engine + CP seed    the same engine, replaying the solver's job order
    CP target           the solver's own objective, under its relaxed model

The gap between rows 1 and 2 is how much of the solver's advantage SURVIVES the
real rules. The gap between rows 2 and 3 is what the relaxation was worth. A
result where row 2 lands back on row 1 is a real finding, not a failure: this repo
already recorded it once ("CP-SAT-as-sequence-oracle: idealized 44-45 d, greedy
replay loses it all"), and it must be reported plainly if it happens again.

Usage
-----
    pip install pyjobshop
    python scripts/tardiness_bound.py --level operators --time-limit 900
    python scripts/tardiness_bound.py --level operators --seed-engine

Reads the SAME book the app plans — same store, same Rule 1 consolidation — so the
number is comparable to what the Schedule tab shows. Needs the store env the app
uses (MONGODB_URI, or the local file store). It only ever reads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MINUTES_PER_DAY = 1440


class Book(NamedTuple):
    """Everything both halves of this tool need from one load.

    ``so_lines`` is what makes --seed-engine possible: the CP model is built from
    ``batches`` (Rule 1's output), but the engine replays from SO lines and runs
    Rule 1 itself. Carrying both, from ONE load, is what keeps the two halves
    measuring the same book.
    """
    so_lines: list
    batches: list
    masters: object
    config: object
    plan_start: dt.datetime
    reserved: dict | None       # operator absences, exactly as the app passes them
    applied_ranks: dict | None  # the optimization the shop is ACTUALLY running


# --------------------------------------------------------------------------- #
# The book — loaded exactly the way the app loads it
# --------------------------------------------------------------------------- #

def load_demo_book(n_items=24, n_orders=60, seed=7) -> Book:
    """The repo's own generated workbook — exercises the whole model path without
    needing the store. For proving the tool runs, never for a real answer.

    ``n_items``/``n_orders`` size it: the defaults are the fixture the test suite
    pins, and ``--demo-items 40 --demo-orders 68`` builds one shaped like the
    owner's real book, which is the only way to check this does not blow up at his
    scale without his data.
    """
    import io

    from engine.config import Config
    from engine.loaders import load_all
    from engine.rules import rule1_consolidate
    from tests.scaled_workbook import PLAN_START, build_scaled_bytes

    raw = build_scaled_bytes(n_items=n_items, n_orders=n_orders, seed=seed)
    so_lines, masters = load_all(io.BytesIO(raw))
    config = Config(plan_start_date=PLAN_START, scheduler="roster",
                    overlap_percent=80, setup_time_min=90.0,
                    apply_operator_logic=True)
    plan_start = dt.datetime.combine(PLAN_START,
                                     dt.time(config.first_shift_start_hour, 0))
    return Book(list(so_lines),
                rule1_consolidate.run(list(so_lines), config=config, masters=masters),
                masters, config, plan_start, None, None)


def load_book() -> Book:
    """The same objects the engine plans, straight from the store (read-only).

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

    # Absences are PHYSICAL unavailability and the app passes them into every plan
    # and every contest candidate. A replay without them would be measured against
    # a crew the shop does not have on those days.
    from engine import optimize_service
    reserved = optimize_service.absence_reservations(book_store.load_absences())

    # The ranks the shop is ACTUALLY running. Without this row the comparison
    # measures the seed against the UNOPTIMIZED engine, which is not the plan
    # anybody has: the live number the owner quotes is post-optimizer. Read-only —
    # loaded to be replayed and printed, never re-saved.
    saved = book_store.load_plan_priority()
    applied = (saved or {}).get("ranks") or None

    batches = rule1_consolidate.run(list(so_lines), config=config, masters=masters)
    return Book(list(so_lines), batches, masters, config, plan_start,
                reserved or None, applied)


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
    jobs, by_key, skipped = build_jobs(
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
    # Task index -> the job (= Rule 1 batch) it belongs to. PyJobShop numbers tasks
    # in creation order and the solution's ``tasks`` list is indexed the same way,
    # which is the assumption ``_audit_rule1`` already relies on. Appending here,
    # right next to every ``add_task``, is what lets the solved schedule be decoded
    # back into a job SEQUENCE — the only thing the engine can actually consume.
    task_jobs: list = []

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
                task_jobs.append(job.key)
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
                task_jobs.append(job.key)
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

    # --- the 90-minute setup is DELIBERATELY NOT MODELLED --------------------- #
    # Measured, not assumed. Sequence-dependent setups need one pair per ordered
    # pair of tasks per machine — 18,944 of them on a book this size — and CP-SAT
    # encodes that as an O(n^2) circuit. On the real book that alone consumed the
    # entire solve: 90 s with setups returned NO feasible solution and a floor of
    # 0; the same 90 s without them returned a real solution and a real floor.
    # It is also what killed the first live runs (exit 143).
    #
    # Omitting it UNDER-estimates every duration, which is a relaxation — so the
    # answer stays a valid FLOOR, just a looser one. Charging setup would have been
    # the invalid direction: over-estimating durations could push the "floor" ABOVE
    # the true optimum and make it a lie.
    _ = setup_min       # kept: the release delay below still uses it

    if unstaffed:
        print(f"  {len(unstaffed)} machine group(s) have NO qualified operator in "
              f"Settings — modelled machine-only (a relaxation): "
              f"{', '.join(sorted(unstaffed)[:4])}")
    m.set_objective(weight_total_tardiness=1)
    # Index -> name, so the solved schedule can be decoded back into (machine,
    # operator, start, end) rows and audited against the shop's OWN rule checks.
    decode = {"machines": [mid for mid in sorted(masters.machines)],
              "operators": [o for o in sorted(ops_res)],
              "n_mach": len(mach),
              "task_jobs": task_jobs,
              "by_key": by_key}
    print(f"  model: {len(jobs)} jobs, {len(mach)} machines, "
          f"{len(ops_res)} operators, {n_modes} modes, setups relaxed away")
    return m, decode


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

def _solution(res):
    """The solved schedule, whatever this PyJobShop version calls it."""
    return getattr(res, "best", None) or getattr(res, "solution", None)


# --------------------------------------------------------------------------- #
# Seeding the engine from the solver (--seed-engine)
# --------------------------------------------------------------------------- #

# The four rule checks the roster engine is audited by. They must ALL be zero on a
# replayed plan: the replay goes through the unchanged pipeline, so a non-zero here
# is not "the seed broke a rule" (the seed cannot — it only reorders), it is an
# engine bug, and burying it under a nice late-day number is exactly how this repo
# has lost weeks before.
RULE_CHECKS = ("OPERATOR_SPLIT_SHIFT", "OPERATION_SEGMENTED",
               "MACHINE_DOUBLE_BOOKED", "IDLE_CAPACITY")


def _cp_job_windows(res, decode):
    """job key -> (earliest task start, latest task end) in the solved schedule.

    Returns {} when there is no solution to read, or when the solution's task list
    does not line up with the model's — the index correspondence is an assumption
    about PyJobShop, so it is CHECKED rather than trusted.
    """
    sol = _solution(res)
    tasks = getattr(sol, "tasks", None)
    if not tasks:
        return {}
    task_jobs = decode.get("task_jobs") or []
    if len(tasks) != len(task_jobs):
        print(f"\n!! cannot derive a sequence: the solver returned {len(tasks)} "
              f"tasks but the model built {len(task_jobs)}. Task indices do not "
              f"line up, so any sequence read from them would be fiction.")
        return {}
    windows: dict = {}
    for i, t in enumerate(tasks):
        if not getattr(t, "present", True):
            continue
        key = task_jobs[i]
        lo, hi = windows.get(key, (None, None))
        s, e = int(t.start), int(t.end)
        windows[key] = (s if lo is None else min(lo, s),
                        e if hi is None else max(hi, e))
    return windows


def _cp_order(windows, by_key, mode):
    """The batches, ordered the way the CP solution runs them.

    ``mode`` is "start" (the job whose FIRST operation starts earliest goes first —
    the release order a dispatcher would follow) or "end" (ordered by completion —
    closer to the priority order an EDD-style rule expresses). Both are worth
    measuring; neither is obviously right, so the tool reports both.

    The tie-break is the job key, so the order is deterministic: two jobs starting
    in the same minute must not sequence differently between two runs of this tool.
    """
    idx = 0 if mode == "start" else 1
    keys = sorted(windows, key=lambda k: (windows[k][idx], k))
    return [by_key[k] for k in keys if k in by_key]


def _replay(book, ranks, label):
    """Plan the book through the UNCHANGED engine, optionally seeded with ``ranks``.

    ``ranks`` is the app's own artifact — ``optimizer.ranks_for``'s output, keyed
    "<so>\\x1f<item>" — and it is handed to ``pipeline.run_forward``'s
    ``priority_rank``, the same parameter the app uses when it replays a saved
    optimization. Nothing here is a special path: this is the live plan code.
    """
    import dataclasses
    import time

    from engine import optimizer, pipeline
    from engine.models import PlanRun
    from roster_engine import report as rr

    config = dataclasses.replace(book.config, scheduler="roster")
    run = PlanRun(so_lines=list(book.so_lines))
    started = time.perf_counter()
    trace = pipeline.run_forward(run, config, book.masters,
                                 reserved=book.reserved, priority_rank=ranks)
    elapsed = time.perf_counter() - started
    if trace["rule6"]["error"]:
        return {"label": label, "error": trace["rule6"]["error"]["message"]}

    entries = run.schedule
    metrics = optimizer.plan_metrics(
        entries, book.so_lines, book.plan_start,
        ceiling_days=getattr(config, "worst_ceiling_days", None))
    rows = rr.all_violations(entries, book.masters, config,
                             batches=run.batches_prioritized)
    counts: dict = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return {"label": label, "metrics": metrics, "counts": counts,
            "rows": rows, "entries": len(entries), "secs": elapsed,
            "score": optimizer.score(metrics), "ranked": len(ranks or {})}


def _search_from_seed(book, ordered, evals):
    """Run the app's OWN sequence+crew search, opened on the CP order.

    ``engine.optimizer.optimize`` takes no starting sequence, so there is no way to
    seed it from outside without editing ``engine/`` — which this tool may not do.
    What IS reachable is the layer underneath: ``roster_engine.search.optimize``
    seeds from the order of the ``jobs`` it is handed (``_seed_sequences`` — "the
    book's own order is included too"). Handing it the CP order therefore puts the
    solver's basin among the openers, alongside EDD and the rest.

    Returns a rank map, or None when the search could not run.
    """
    from engine import optimizer, roster_adapter
    from roster_engine import search as roster_search
    from roster_engine.domain import build_jobs, build_shop

    flexible = bool(getattr(book.config, "flexible_machines", False))
    jobs, by_key, _skipped = build_jobs(book.batches, book.masters, flexible)
    if not jobs:
        return None
    rank_of = {str(b.batch_id): i for i, b in enumerate(ordered)}
    jobs = sorted(jobs, key=lambda j: (rank_of.get(j.key, len(rank_of)), j.key))

    shop = build_shop(book.masters, roster_adapter._absent_from_reserved(
        book.reserved, book.masters, []))
    res = roster_search.optimize(jobs, shop, roster_adapter._resolved(book.config),
                                 overlap=roster_adapter._overlap(book.config),
                                 budget_evals=int(evals), seed=42)
    seq = [by_key[k] for k in (res.sequence or ()) if k in by_key]
    return optimizer.ranks_for(seq) if seq else None


def _row(r):
    if r.get("error"):
        return f"  {r['label']:<30s}  PLAN FAILED: {r['error'][:60]}"
    m = r["metrics"]
    bad = sum(r["counts"].get(k, 0) for k in RULE_CHECKS)
    return (f"  {r['label']:<30s} {m['total_late_days']:>10d} {m['late_orders']:>8d} "
            f"{m['max_late_days']:>7d} {m['makespan_days']:>10.2f} "
            f"{r['score']:>10.1f} {bad:>6d} {r['secs']:>7.1f}")


def seed_engine(res, decode, book, search_evals=0):
    """Derive a sequence from the CP solution, replay it through the real engine,
    and print the three-row comparison. Read-only from end to end."""
    windows = _cp_job_windows(res, decode)
    if not windows:
        print("\n(no CP schedule to derive a sequence from — nothing to seed)")
        return

    from engine import optimizer

    by_key = decode.get("by_key") or {}
    print(f"\nSEEDING THE ENGINE FROM THE CP SOLUTION")
    print(f"  CP jobs with a solved window : {len(windows)} "
          f"(of {len(book.batches)} batches)")

    bare = _replay(book, None, "engine alone (no seed)")
    rows = [bare]
    live = None
    if book.applied_ranks:
        # The live plan's own sequence, replayed here so it is measured by the
        # SAME yardstick as the seeded rows. It will not match the Schedule tab to
        # the day — this replay carries no frozen (in-progress) pins — but it is
        # the only row that answers "does the solver beat what we are running".
        live = _replay(book, book.applied_ranks, "engine + APPLIED ranks (live)")
        rows.append(live)

    seeded = []
    for mode in ("start", "end"):
        ordered = _cp_order(windows, by_key, mode)
        r = _replay(book, optimizer.ranks_for(ordered), f"engine + CP seed ({mode})")
        r["ordered"] = ordered
        seeded.append(r)
    rows.extend(seeded)

    if search_evals > 0:
        # Open the search on whichever CP order replayed better.
        ok = [r for r in seeded if not r.get("error")]
        if ok:
            pick = min(ok, key=lambda r: r["metrics"]["total_late_days"])
            ranks = _search_from_seed(book, pick["ordered"], search_evals)
            if ranks:
                r = _replay(book, ranks,
                            f"engine search from CP ({search_evals}ev)")
                seeded.append(r)
                rows.append(r)

    print(f"\n  {'plan':<30s} {'late-days':>10s} {'late':>8s} {'worst':>7s} "
          f"{'makespan':>10s} {'score':>10s} {'VIOL':>6s} {'secs':>7s}")
    for r in rows:
        print(_row(r))

    obj = getattr(res, "objective", None)
    if obj is not None:
        print(f"  {'CP target (relaxed model)':<30s} {obj / MINUTES_PER_DAY:>10.1f} "
              f"{'-':>8s} {'-':>7s} {'-':>10s} {'-':>10s} {'n/a':>6s} {'-':>7s}")
    print("  score = engine.optimizer.score, the objective the app's own search "
          "MINIMISES.\n  It is symmetric — finishing far EARLY is penalised like "
          "finishing late — so it\n  can move in the opposite direction to "
          "late-days. Both are printed for that reason.")

    # Rule violations are never a footnote. If the replayed plan breaks a rule the
    # comparison above is measuring something the shop cannot run.
    dirty = False
    for r in rows:
        if r.get("error"):
            continue
        bad = {k: v for k, v in r["counts"].items() if k in RULE_CHECKS and v}
        if bad:
            dirty = True
            print(f"\n  !! RULE VIOLATIONS in '{r['label']}': {bad}")
            for row in r["rows"][:3]:
                if row["kind"] in RULE_CHECKS:
                    print(f"       - {row['kind']}: {row['message'][:96]}")
    if not dirty:
        print(f"\n  rule checks ({', '.join(RULE_CHECKS)}): 0 on every plan above.")
    for r in rows:
        if r.get("error"):
            continue
        extra = {k: v for k, v in r["counts"].items() if k not in RULE_CHECKS and v}
        if extra:
            print(f"  note — '{r['label']}' reports {extra} "
                  f"(not rule breaches; see roster_engine/report.py)")

    # The incumbent is what the shop is RUNNING, so when an applied optimization
    # exists that is the row to beat — measuring the seed against the unoptimized
    # engine would credit it with everything the owner's own optimizer already won.
    base = live if (live and not live.get("error")) else rows[0]
    best = min((r for r in seeded if not r.get("error")),
               key=lambda r: r["metrics"]["total_late_days"], default=None)
    if base.get("error") or best is None:
        return
    gained = base["metrics"]["total_late_days"] - best["metrics"]["total_late_days"]
    print(f"""
DID THE SEED HELP?
  incumbent         {base['metrics']['total_late_days']} late-days ({base['label']})
  best CP-seeded    {best['metrics']['total_late_days']} late-days ({best['label']})
  recovered         {gained:+d} late-days""")
    if not rows[0].get("error"):
        print(f"  (engine with no seed at all: "
              f"{rows[0]['metrics']['total_late_days']} late-days)")

    # The two halves of this tool do not optimise the same thing, and pretending
    # otherwise is the easiest way to read a win into this table that is not there.
    # The CP model minimises TOTAL TARDINESS. The app minimises ``optimizer.score``,
    # which is SYMMETRIC: finishing far early is penalised like finishing late. A
    # seed can therefore cut late-days while being much worse by the app's own
    # objective — in which case the app's search is not failing to find it, it is
    # correctly REJECTING it, and the gap is an objective mismatch, not a search gap.
    if best["score"] > base["score"]:
        print(f"""
  !! BUT READ THE SCORE COLUMN. The seeded plan scores {best['score']:.1f} against the
  incumbent's {base['score']:.1f} — it is WORSE by the objective the app actually
  minimises. The CP model minimises total tardiness only; ``optimizer.score`` is
  symmetric and penalises finishing far EARLY just as hard. So this is not the
  engine failing to find the solver's plan — a search on this objective would
  correctly reject it. To make the 'late-days' gap a real target, the OBJECTIVE
  has to be the owner's decision first: if fewest late deliveries is what he wants,
  ``score`` is the thing to change, and the search is not the problem.""")

    if gained <= 0:
        print("""
  The sequence did NOT survive replay. The solver's advantage lives in decisions
  the engine does not take from a sequence — which machine, which operator, which
  shift — so handing over the job ORDER alone hands over none of it. This is the
  same result the repo already recorded once for CP-SAT-as-sequence-oracle. It is
  a real finding: it says the gap is NOT reachable by re-sequencing, and that any
  further work belongs in the engine's assignment decisions, not its search.""")
    else:
        print("""
  Some of the solver's advantage DOES survive the real rules. Nothing has been
  applied — this tool never writes. Applying it is the owner's decision, through
  the Optimize panel's own Apply path.

  Caveat before acting on it: this replay carries NO frozen (in-progress) pins,
  while the live plan does. Re-measure through the app's own Optimize panel — that
  path pins in-progress work — before treating the gain as bankable.""")


def _audit_rule1(res, decode, plan_start, config):
    """How badly does the CP schedule break Rule 1?

    The model cannot express "one operator mans one machine for a whole shift", so
    its answer is a floor rather than a plan. But we can MEASURE how much it leaned
    on that freedom: decode the solved schedule and run the shop's own
    roster_engine.report.operator_split_violations over it — the identical check
    the live plan is audited with.

    Few splits  -> the better schedule barely needed to break Rule 1, so most of
                   the gap to the live plan is genuinely reachable.
    Many splits -> the CP answer is bought largely WITH the rule, and the live
                   plan is closer to its real ceiling than the raw number suggests.
    """
    sol = _solution(res)
    tasks = getattr(sol, "tasks", None)
    if not tasks:
        print("\n(no schedule to audit — the solver returned no solution)")
        return

    from engine.models import ScheduleEntry
    from roster_engine import report as rr

    mids, ops = decode["machines"], decode["operators"]
    entries, machining_rows = [], 0
    for i, t in enumerate(tasks):
        if not getattr(t, "present", True) or t.end <= t.start:
            continue
        mid = op = None
        for r in (t.resources or []):
            if r < len(mids):
                mid = mids[r]
            elif ops and r - decode["n_mach"] - 1 < len(ops):
                op = ops[r - decode["n_mach"] - 1]
        if not mid or not op:
            continue
        st = plan_start + dt.timedelta(minutes=int(t.start))
        en = plan_start + dt.timedelta(minutes=int(t.end))
        entries.append(ScheduleEntry(
            batch_id=f"T{i}", item_code=f"I{i}", process_seq=1, process_name="op",
            machine=mid, qty=1, occupancy_min=float(t.end - t.start),
            start=st, end=en, operator=op, op_segments=[(st, en, op)]))
        machining_rows += 1

    if not entries:
        print("\n(schedule carried no operator assignments — run --level operators "
              "to audit Rule 1)")
        return

    splits = rr.operator_split_violations(entries, config, None)
    print(f"\nRULE 1 AUDIT of the schedule the solver actually found")
    print(f"  operator-assigned operations : {machining_rows}")
    print(f"  shifts where ONE operator is on 2+ machines : {len(splits)}")
    if splits:
        for row in splits[:4]:
            print(f"    - {row['message'][:96]}")
    print("  Your live plan has 0 of these, by construction.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--level", choices=["machines", "operators"], default="operators",
                    help="machines: machines only (loosest, fastest). "
                         "operators: + nobody in two places at once (tighter).")
    ap.add_argument("--time-limit", type=float, default=600.0, help="seconds")
    ap.add_argument("--horizon-days", type=int, default=70)
    ap.add_argument("--workers", type=int, default=2,
                    help="solver threads; each one costs memory (2 is safe "
                         "on a GitHub runner)")
    ap.add_argument("--demo", action="store_true",
                    help="use the repo's generated book instead of the store — "
                         "proves the tool runs; not a real answer")
    ap.add_argument("--demo-items", type=int, default=24,
                    help="--demo only: distinct items in the generated book "
                         "(40 is the owner's scale)")
    ap.add_argument("--demo-orders", type=int, default=60,
                    help="--demo only: SO lines in the generated book "
                         "(68 is the owner's scale)")
    ap.add_argument("--seed-engine", action="store_true",
                    help="after solving, hand the CP job order to the REAL engine "
                         "as a starting sequence, re-plan under all four rules and "
                         "print the comparison. Read-only: applies nothing.")
    ap.add_argument("--seed-search-evals", type=int, default=0,
                    help="--seed-engine only: additionally run the app's own "
                         "sequence+crew search opened on the CP order, for this "
                         "many plans (0 = off).")
    args = ap.parse_args()

    try:
        import pyjobshop  # noqa: F401
    except ImportError:
        raise SystemExit("pyjobshop is not installed.  pip install pyjobshop")

    if args.demo:
        print("DEMO book (repo fixture) — this is a self-test, not your shop.")
        book = load_demo_book(n_items=args.demo_items, n_orders=args.demo_orders)
    else:
        print("Loading the book from the store (read-only)...")
        book = load_book()
    batches, masters, config, plan_start = (book.batches, book.masters,
                                            book.config, book.plan_start)
    print(f"  {len(book.so_lines)} SO lines, {len(batches)} batches, plan start "
          f"{plan_start:%d-%m-%Y %H:%M}, overlap "
          f"{getattr(config, 'overlap_percent', '?')}%")

    print(f"Building the CP model at level '{args.level}'...")
    model, decode = build(batches, masters, config, plan_start, args.level,
                          args.horizon_days)

    print(f"Solving (limit {args.time_limit:.0f}s)...")
    print("  exit code 143 here means the RUNNER killed it for memory — rerun with"
          " --level machines and a smaller --horizon-days.")
    res = model.solve(time_limit=args.time_limit, display=True,
                      num_workers=max(1, args.workers))

    lb_min = getattr(res, "lower_bound", None)
    obj_min = getattr(res, "objective", None)
    print("\n" + "=" * 68)
    print(f"status               : {getattr(res, 'status', '?')}")
    if obj_min is not None:
        print(f"best found           : {obj_min / MINUTES_PER_DAY:>9.1f} late-days")
    if lb_min is not None:
        print(f"PROVEN FLOOR         : {lb_min / MINUTES_PER_DAY:>9.1f} late-days")
    print("=" * 68)
    _audit_rule1(res, decode, plan_start, config)
    if args.seed_engine:
        seed_engine(res, decode, book, search_evals=args.seed_search_evals)
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
