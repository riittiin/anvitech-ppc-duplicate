"""Task 11 — the whole app, planning on the roster engine, on the shipped book.

This is the test that catches wiring breaks the unit tests cannot see: a plan
that runs but produces entries the Gantt cannot draw, a report that crashes, or
a step the plan quietly gives fewer pieces than its batch owes.

TWO BOOKS, and why both are here:

* the repo's own ``tests/sample_workbook.py`` — 3 SO lines over 2 items, which
  plan to **5 schedule entries**. That is enough to prove the app wires up and
  every downstream surface renders, and far too little for a violation check to
  have anything to look at (measured: ``overlap_rounding_scan`` detects 0 on it,
  so an assertion that it reports 0 would be vacuous).
* ``tests/scaled_workbook.py`` — a SYNTHETIC 60-line book shaped like the
  owner's shop, shared with ``scripts/roster_vs_new.py`` so the table posted to
  the owner and the tests that pin its headline are the same book. **It is not
  the owner's book**; no number from it forecasts anything about Anvitech.

THREE FIXTURE DECISIONS, all deliberate and recorded so a later reader does not
mistake any of them for a fudge:

1. **One operator is added in Settings, on the SAMPLE book only.**
   ``tests/sample_workbook.py`` as shipped staffs CNC1/CNC2/BS1 only — ``MI1``
   and the documented provisional ``CNC9`` have nobody, so the roster engine
   (correctly) refuses the book with a typed ``RuleError``. That refusal is
   already pinned by ``test_roster_wiring.py::
   test_an_unstaffable_step_is_a_typed_rule_error_not_an_exception``; this file
   needs a book that PLANS. Operators are the SETTINGS table, not the workbook
   sheet (CLAUDE.md), so adding one here is exactly what an admin does in
   Settings — it is not a change to the workbook, and the same helper already
   exists in ``test_roster_wiring._fully_staffed``. The scaled book staffs every
   machine from its own sheet and needs no such addition.

2. **``new_engine`` reads its masters from the STORE, not from the ``masters``
   argument.** So the non-vacuity tests below have to save the same workbook
   bytes through ``book_store`` first (the per-test store is isolated by
   conftest). Without that priming it does not crash — MEASURED: it returns an
   EMPTY schedule with no note and no error, and every assertion about it would
   pass vacuously. ``_plan`` therefore asserts the entry list is non-empty.

3. **The scaled book's bench crew is bench-only.** See
   ``tests/scaled_workbook.py``.

NON-VACUITY — the thing this file exists to protect, argued three independent
ways so that no single future change can quietly hollow it out:

  (a) ``test_the_live_engine_is_not_clean_on_the_same_books`` — the live ``new``
      engine, planning the SAME books under the SAME config, is measured dirty
      on checks the roster plan reads zero on. Read its docstring before
      trusting the SPLIT column: over 8 seeds ``new`` scores 1/2/0/2/3/0/0/1
      splits, i.e. **three of eight books produce none**, so the split signal is
      genuinely fragile on one book. The rounding-reported signal is not (5-12 on
      every one of the 8), and both are asserted at the strength they were
      measured at.
  (b) ``test_the_split_check_fires_on_a_deliberately_corrupted_roster_plan`` —
      the check has teeth without depending on any engine misbehaving, so if the
      live engine is ever fixed the zeros here still mean something.
  (c) the whole-plan invariants were confirmed to fire on hand-corrupted copies
      of this very plan (1 row for a shorted quantity, 4 for an inverted routing
      step) before being asserted empty on the real one.

(The 2026-08-09 lesson: this fixture family passes vacuously by default.)
"""
import dataclasses
import io
from datetime import date

import pytest

from engine import book_store, gantt, new_engine, pipeline
from engine.analytics import build_analytics
from engine.config import Config
from engine.loaders import load_all
from engine.models import Operator, PlanRun
from engine.new_engine import (batch_quantity_violations,
                               routing_order_violations)
from roster_engine import report as rreport
from tests.sample_workbook import build_sample_bytes
from tests.scaled_workbook import build_scaled_bytes

PLAN_START = date(2026, 8, 12)

# The eight synthetic books the cross-engine comparison is measured over. Eight,
# not one: a single book made the SPLIT signal look robust when it is not.
SCALED_SEEDS = (1, 2, 3, 4, 5, 6, 7, 8)


def _fully_staffed(masters):
    """Add the ONE Settings operator the shipped workbook is missing (see the
    module docstring). Identical to ``test_roster_wiring._fully_staffed``."""
    ops = list(masters.operators) + [
        Operator(name="Operator Four", preferred_machines_raw="MI1/MW1/CNC9",
                 machines=["MI1", "MW1", "CNC9"], shift="First shift")]
    return dataclasses.replace(masters, operators=ops)


@pytest.fixture
def book():
    """(workbook bytes, so_lines, fully-staffed masters)."""
    raw = build_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    return raw, so_lines, _fully_staffed(masters)


def scaled_book(seed):
    """The same shop-sized book ``scripts/roster_vs_new.py`` measures on, in the
    ``(raw, so_lines, masters)`` shape ``_plan`` takes. Its own operator sheet
    staffs every machine, so no Settings addition is needed or made."""
    raw = build_scaled_bytes(seed=seed)
    so_lines, masters = load_all(io.BytesIO(raw))
    return raw, so_lines, masters


def _config(scheduler="roster"):
    return Config(plan_start_date=PLAN_START, scheduler=scheduler,
                  overlap_percent=80, setup_time_min=90.0,
                  apply_operator_logic=True)


def _plan(book, scheduler="roster"):
    """Plan the book through the REAL pipeline. Returns
    ``(plan_run, config, entries)`` — ``trace[...]["output"]`` is a display TABLE,
    not the rule's objects, so the objects come off the PlanRun."""
    raw, so_lines, masters = book
    if scheduler == "new":
        # See the module docstring: the new engine loads routings/machines from
        # the stored workbook, never from `masters`.
        book_store.save_masters_bytes(raw)
        new_engine.set_masters_bytes(None)
    config = _config(scheduler)
    run = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(run, config, masters)
    assert trace["rule6"]["error"] is None, trace["rule6"]["error"]
    # An unprimed `new` engine returns an EMPTY schedule with no error (measured),
    # which would make every assertion downstream of it pass vacuously.
    assert run.schedule, f"{scheduler} planned nothing"
    return run, config, run.schedule


# --------------------------------------------------------------------------- #
# The plan itself
# --------------------------------------------------------------------------- #

def test_the_book_plans_on_the_roster_engine(book):
    _run, _config_, entries = _plan(book)
    assert entries
    # Every real-machine entry names an operator and carries the segments the four
    # checks are measured on — WITHOUT which those checks are structurally blind.
    # NOTE this does not by itself prove the roster engine ran: the classic engine
    # with apply_operator_logic on satisfies it too (verified by mutation). WHICH
    # engine ran is pinned by call-count in
    # test_roster_wiring.py::test_run_forward_really_reaches_the_roster_adapter;
    # what this file's mutation run showed is that swapping the dispatch to classic
    # is caught here by test_the_roster_plan_has_no_rule_violations, which fails
    # with a real 3-machine split.
    for entry in entries:
        if entry.machine not in rreport.OFF_LANES:
            assert entry.operator_label(), entry
            assert entry.op_segments, entry


def test_the_roster_plan_has_no_rule_violations(book):
    """The whole point. Zero of all four, on a book the app really planned."""
    _run, config, entries = _plan(book)
    _raw, _so, masters = book
    rows = rreport.all_violations(entries, masters, config)
    assert rows == [], "\n".join(r["message"] for r in rows[:10])


# --------------------------------------------------------------------------- #
# The shop-sized book — the same one scripts/roster_vs_new.py posts a table from
# --------------------------------------------------------------------------- #

def test_the_roster_plan_is_clean_on_eight_shop_sized_books():
    """All four rules, on 8 x 60-line books of ~160 entries each — not the
    5-entry sample. ``OVERLAP_FRACTIONAL_PIECE`` is asserted on ``reported`` and
    the whole triple is carried in the failure message, because a bare 0 there is
    NOT "this engine releases on whole pieces" (see ``roster_engine.report``).
    ``detected == muted + reported`` pins the accounting so a future change that
    starts muting everything is visible rather than silent."""
    seen = {}
    for seed in SCALED_SEEDS:
        bk = scaled_book(seed)
        _run, config, entries = _plan(bk)
        masters = bk[2]
        scan = rreport.overlap_rounding_scan(entries, masters, config)
        seen[seed] = (len(entries), scan["detected"], scan["muted"],
                      scan["reported"])
        assert rreport.all_violations(entries, masters, config) == [], (
            f"seed {seed}: "
            + "\n".join(r["message"] for r in
                        rreport.all_violations(entries, masters, config)[:5]))
        assert scan["detected"] == scan["muted"] + scan["reported"], seen
        assert scan["reported"] == 0, (seed, scan["rows"])
    # Non-vacuous in the only sense that matters here: there were entries to look
    # at on every book.
    assert all(n > 100 for n, *_ in seen.values()), seen


def test_the_whole_plan_invariants_hold_on_the_shop_sized_book():
    """The app's own two cross-engine invariants, on a book big enough for them
    to have something to check. Both were confirmed to FIRE on hand-corrupted
    copies of this plan (1 row for a shorted quantity, 4 for an inverted routing
    step) — they are not returning [] because they look at nothing."""
    for seed in SCALED_SEEDS[:3]:
        bk = scaled_book(seed)
        run, _config_, entries = _plan(bk)
        assert batch_quantity_violations(entries, run.batches) == [], seed
        assert routing_order_violations(entries, bk[2]) == [], seed


# --------------------------------------------------------------------------- #
# Non-vacuity — three ways, on purpose
# --------------------------------------------------------------------------- #

def test_the_live_engine_is_not_clean_on_the_same_books():
    """The cross-engine half of the argument, MEASURED over 8 books, and the two
    signals are NOT equally strong — this test asserts each at the strength it
    was actually measured at, which is the whole point of it existing.

    Measured 2026-08-13, ``new`` vs ``roster`` on the same 8 books, same config:

        seed          1   2   3   4   5   6   7   8
        new  SPLIT    1   2   0   2   3   0   0   1     <- FRAGILE: 0 on 3 of 8
        new  ROUND-r  7   6   9  12   7   6   5   5     <- fires on every book
        roster SPLIT  0   0   0   0   0   0   0   0
        roster ROUND-r 0  0   0   0   0   0   0   0

    So: the SPLIT column is asserted only in AGGREGATE (at least one hop
    somewhere across the 8), because on any single book it is a coin-flip and a
    per-seed assertion would be a flake waiting to happen. The rounding-reported
    column is asserted per book, because it was measured on every one.

    If a future change makes ``new`` clean on all 8, do NOT delete this quietly:
    the roster plan's zeros would then rest solely on
    ``test_the_split_check_fires_on_a_deliberately_corrupted_roster_plan``, and
    that trade has to be said out loud.
    """
    splits, rounds = {}, {}
    for seed in SCALED_SEEDS:
        bk = scaled_book(seed)
        _run, config, entries = _plan(bk, scheduler="new")
        masters = bk[2]
        splits[seed] = len(
            rreport.operator_split_violations(entries, config, masters))
        rounds[seed] = rreport.overlap_rounding_scan(
            entries, masters, config)["reported"]
    assert sum(splits.values()) >= 1, (
        f"the live engine hopped nobody on ANY of the 8 books: {splits}")
    assert all(v >= 1 for v in rounds.values()), (
        f"the fractional-release check reported nothing on some book: {rounds}")


def test_the_idle_capacity_check_can_fire_on_this_fixture():
    """``IDLE_CAPACITY`` reads 0 for BOTH roster and new on all 8 books, and the
    standing warning (Task 8) is that a 0 there usually means the FIXTURE has no
    spare operator rather than that there is no idle capacity to find. This test
    settles that question for THIS fixture by planning the retired ``classic``
    engine on the same 8 books: measured 2026-08-13 it yields
    ``[6, 0, 4, 3, 5, 3, 3, 5]`` — 29 rows over 7 of the 8. So the fixture does
    carry spare qualified crew that the check can see, and the roster/new zeros
    on it are MEASURED zeros rather than blind ones.

    What this does NOT show: classic is retired and builds a differently shaped
    plan, so this proves the CHECK has teeth on this book — not that the roster
    engine would read 0 on the owner's real one, which no test in this repo can
    reach.
    """
    fired = {}
    for seed in SCALED_SEEDS:
        bk = scaled_book(seed)
        _run, config, entries = _plan(bk, scheduler="classic")
        fired[seed] = len(
            rreport.idle_capacity_violations(entries, bk[2], config))
    assert sum(fired.values()) >= 1, (
        f"no engine leaves idle capacity on this fixture, so a 0 in that column "
        f"is unmeasured, not clean: {fired}")

def test_the_live_engine_violates_rule_1_on_the_same_book(book):
    """MEASURED, not assumed: on this book the live ``new`` engine puts Operator
    Four on CNC9 (08:00-09:46) and MI1 (10:24-10:54) inside ONE first shift.

    If the live engine were clean too, the four checks would be measuring nothing
    and the roster plan's zero would prove nothing. Should a future change make
    ``new`` clean here, do NOT delete this test quietly — replace the argument,
    e.g. with the corrupted-plan test below, and say so.
    """
    _run, config, entries = _plan(book, scheduler="new")
    assert entries, "the new engine planned nothing — was the store primed?"
    _raw, _so, masters = book
    rows = rreport.operator_split_violations(entries, config, masters)
    assert rows, "expected the live engine to hop one operator between machines"


def test_the_split_check_fires_on_a_deliberately_corrupted_roster_plan(book):
    """The engine-independent half of the non-vacuity argument: take the CLEAN
    roster plan and move one machining entry onto an operator who is already on
    another machine that shift. The check must notice. If it does not, its zero
    above means nothing whatever the live engine does."""
    _run, config, entries = _plan(book)
    _raw, _so, masters = book
    machining = [e for e in entries
                 if e.machine.upper().startswith(("CNC", "VMC")) and e.op_segments]
    assert len(machining) >= 2, "fixture no longer has two machining entries"
    victim, other = machining[0], machining[1]
    assert victim.machine != other.machine
    stolen = other.op_segments[0][2]
    corrupted = [dataclasses.replace(
        e, operator=stolen,
        op_segments=[(s, t, stolen) for s, t, _w in e.op_segments])
        if e is victim else e for e in entries]
    # Same shift, two machines, at least one of them a CNC/VMC.
    rows = rreport.operator_split_violations(corrupted, config, masters)
    assert rows, "the split check did not see a hand-made two-machine shift"
    assert stolen in rows[0]["message"]


# --------------------------------------------------------------------------- #
# Every downstream surface must render it
# --------------------------------------------------------------------------- #

def test_the_gantt_renders_the_roster_plan(book):
    run, _config_, entries = _plan(book)
    _raw, _so, masters = book
    view = gantt.build_gantt(entries, run.batches, masters)
    assert view["rows"]
    assert view["num_days"] > 0 and view["axis_start"]
    assert all(row.get("bars") for row in view["rows"])


def test_analytics_renders_the_roster_plan(book):
    run, config, entries = _plan(book)
    _raw, _so, masters = book
    out = build_analytics(entries, masters, config, run.batches)
    assert out["machines"] and out["operators"] and out["processes"]
    assert out["headline"]
    # Seeded from the MASTERS, not the schedule (2026-08-07): a machine or a
    # person the plan gave no work to is still a row. Machines are listed by
    # DISPLAY name ("CNC 1"), so they are counted rather than compared by id.
    assert len(out["machines"]) >= len(masters.machines)
    assert ({o["Operator"] for o in out["operators"]}
            >= {o.name for o in masters.operators})


# --------------------------------------------------------------------------- #
# The two whole-plan invariants the app already runs on every plan
# --------------------------------------------------------------------------- #

def test_every_batch_is_fully_produced(book):
    """No step may be given fewer pieces than its batch owes (2026-08-11)."""
    run, _config_, entries = _plan(book)
    assert batch_quantity_violations(entries, run.batches) == []


def test_routing_order_is_never_inverted(book):
    _run, _config_, entries = _plan(book)
    _raw, _so, masters = book
    assert routing_order_violations(entries, masters) == []


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #

def _signature(entries):
    return [(e.batch_id, e.process_seq, e.machine, e.start, e.end, e.qty,
             e.occupancy_min, e.operator_label(), tuple(e.op_segments or ()))
            for e in entries]


def test_the_plan_is_reproducible(book):
    """The optimizer builds thousands of plans and compares them; an engine that
    is not a function of its inputs makes every one of those comparisons noise."""
    first = _signature(_plan(book)[2])
    second = _signature(_plan(book)[2])
    assert first and first == second


# --------------------------------------------------------------------------- #
# The shop-sized re-plan WITH WORK IN PROGRESS
#
# Nothing else in this repo plans a shop-sized book through `run_forward` with a
# real frozen set and then checks it. This builds the whole live path — plan
# clean -> `freeze.schedule_projection` -> punch part of the pieces -> `freeze.
# compute_frozen_set` -> re-plan — because that is the shape of BOTH defects
# that actually reached production, and neither was reachable without WIP:
#
#   * 2026-08-09, the routing inversion: the frozen pre-placement had no
#     precedence gate at all, so CNC FIRST SIDE ran two days AFTER every step
#     that eats its output. Clean book: 0 violations. With work in progress:
#     67 of 68 orders.
#   * 2026-08-11, the clubbed-batch quantity: a frozen row's per-SO-LINE
#     remainder became the BATCH operation's quantity, and 281 pieces of a
#     clubbed order landed in no plan at all. Needed WIP *and* a clubbed batch.
#
# The reviewer ran this path and it is clean, so this pins a passing result
# rather than chasing a bug — the cheapest available insurance against the two
# classes that have actually cost the owner a plan.
# --------------------------------------------------------------------------- #

from collections import defaultdict

from engine import freeze, loaders, orderbook
from engine.models import Actual, Order


def _wip_book(seed, every=2, fractions=(0.6, 0.3)):
    """(so_lines, masters, frozen rows) for a book part-way through production.

    The punches go through the ORDER BOOK's own accounting — ``Actual`` rows ->
    ``orderbook.active_so_lines`` -> ``freeze.compute_frozen_set`` — not a
    hand-built frozen list, so this exercises the same translation the app does
    (and the same one the 2026-08-11 bug hid in).

    TWO steps are punched, not one, and that is the whole fixture. A book whose
    only frozen steps are each routing's FIRST step cannot see the 2026-08-09
    inversion at all: the routing gate it tests has nothing upstream to gate
    against, and deleting that gate from the frozen path leaves this file green
    (verified by mutation). Punching step 1 at 60% and step 2 at 30% leaves BOTH
    frozen, with step 2 pinned to its machine while the step that feeds it still
    owes 40% of the batch — which is exactly the live shape.
    """
    raw, so_lines, masters = scaled_book(seed)
    _run, _config, entries = _plan((raw, so_lines, masters))
    applied = freeze.schedule_projection(entries)

    orders, actuals = {}, []
    for i, line in enumerate(so_lines):
        order = Order(so_no=line.so_no, item_code=line.item_code,
                      item_name=line.item_name, ordered_qty=line.qty,
                      delivery_date=line.delivery_date)
        orders[order.key] = order
        routing = masters.routings.get(line.item_code)
        if routing is None or i % every:
            continue
        # PART of a step: good > 0 and remaining > 0 is exactly what
        # `compute_frozen_set` freezes. A full punch would finish the step, and a
        # zero punch would not start it — neither is in progress. The second
        # step's punch is the smaller one, so downstream never exceeds upstream
        # (the feedback precedence rule the app enforces at capture).
        steps = sorted(routing.processes, key=lambda p: p.seq)[:len(fractions)]
        made = [max(1, int(line.qty * f)) for f in fractions]
        if len(steps) < len(fractions) or max(made) >= line.qty:
            continue
        for proc, qty in zip(steps, made):
            actuals.append(Actual(so_no=line.so_no, item_code=line.item_code,
                                  entry_date=PLAN_START, qty_produced=float(qty),
                                  process=proc.name, shift="1st shift",
                                  operator="Anturam"))

    wip_lines = orderbook.active_so_lines(orders, actuals, masters)
    good = defaultdict(float)
    for a in actuals:
        good[(a.so_no, a.item_code,
              loaders.normalize_process_name(a.process))] += a.qty_produced
    frozen = freeze.compute_frozen_set(applied, wip_lines, dict(good), masters)
    return wip_lines, masters, frozen


def test_a_shop_sized_book_re_plans_cleanly_with_work_in_progress():
    """The whole live re-plan path, checks and all. Every assertion here is one
    a production incident has already been lost to."""
    for seed in SCALED_SEEDS[:3]:
        wip_lines, masters, frozen = _wip_book(seed)
        assert frozen, f"seed {seed}: nothing was in progress — the fixture is vacuous"

        config = _config()
        run = PlanRun(so_lines=list(wip_lines))
        trace = pipeline.run_forward(run, config, masters, frozen=frozen)
        assert trace["rule6"]["error"] is None, trace["rule6"]["error"]
        assert run.schedule, f"seed {seed}: planned nothing"

        # 2026-08-09 — a step never runs before the step that feeds it.
        assert routing_order_violations(run.schedule, masters) == [], seed
        # 2026-08-11 — no step is given fewer pieces than its batch owes.
        assert batch_quantity_violations(run.schedule, run.batches) == [], seed
        # ...and the four roster rules still hold with pins in place.
        rows = rreport.all_violations(run.schedule, masters, config)
        assert rows == [], (seed, [r["message"] for r in rows[:5]])


def test_the_frozen_pins_really_landed_on_the_shop_sized_re_plan():
    """Non-vacuity for the test above, in the direction that matters: a re-plan
    that quietly dropped every pin would satisfy all three checks and prove
    nothing about frozen work at all (measured against the real producer's row
    shape, the drop rate was once 100%). Every in-progress operation must be
    planned on the machine it is physically on."""
    for seed in SCALED_SEEDS[:3]:
        wip_lines, masters, frozen = _wip_book(seed)
        notes = []
        run = PlanRun(so_lines=list(wip_lines))
        pipeline.run_forward(run, _config(), masters, frozen=frozen)
        from engine.rules import rule1_consolidate
        batches = rule1_consolidate.run(list(wip_lines), config=_config(),
                                        masters=masters)
        from engine import roster_adapter
        jobs, batch_by_key, _sk = roster_adapter.build_jobs(batches, masters)
        pins = roster_adapter._pins(frozen, batch_by_key, masters, notes)
        assert pins, seed
        placed = {(e.batch_id, e.process_seq): e.machine for e in run.schedule}
        for pin in pins:
            key = (pin["order_key"], pin["op_seq"])
            assert placed.get(key) == pin["machine_id"], (seed, pin, placed.get(key))
        assert not [n for n in notes if "could not be matched" in n], notes
