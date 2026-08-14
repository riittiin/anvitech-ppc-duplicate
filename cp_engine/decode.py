"""Genome + today's book -> laid-out times. THIS MODULE DECIDES NOTHING.

Every choice — which machine, which operator, which order, how many pieces
release the successor — is read from the genome. What is computed here is only
WHEN, and it is computed exactly: the release is worked minutes, so an overnight
gap never releases pieces that were never cut.

That exactness is deliberate and is one half of a pair. The CP model's release
bounds are wall-clock and therefore approximate (spec §5.3); this side is the
truth, and cp_engine.report.completion_drift measures the difference rather than
assuming it away. If drift is material, tighten the model — never loosen this.

Adapted from the sibling greedy roster-first engine's shift clock, with the
CHOOSING helpers removed (its job pick, its bench pick, its operator preference,
its shift-demand projection and its roster call). Referred to obliquely, as the
rest of this package does, because the package must stand alone and a test scans
these files for the sibling's name. Runs on Render: no pyjobshop import, at any
depth.

**The solved START is a HARD FLOOR, and that is what makes the replay the same
plan.** A single global job rank cannot reproduce a solve: the solver's real
decision is a PER-MACHINE SEQUENCE with starts it was free NOT to left-shift, and
a greedy dispatcher re-derives neither. Measured, before ``cp_start_of`` existed:
12 books solved OPTIMAL and replayed drifted 1-7 completion-days each, with two
machines' op order differing from the solve. So every op that the solve placed
carries its solved start, and the decoder takes ``max(floor, earliest feasible)``
— on the book that was solved the solve is feasible, so each op lands exactly on
it, and the per-machine sequence comes back for free because solved starts on one
machine do not overlap. No separate sequence key is needed, and none is added.

The conservatism is deliberate: a completed order frees capacity and nobody moves
up into it. That matches this repo's own 2026-08-09 finding — pulling work earlier
is not monotonically good, and gap backfill cost the owner ~40 late-days. Orders
the solve never saw carry no floor and place freely.

MEASURED RESIDUAL, and its cause, so nobody has to rediscover it. Over 40 solved
books (280 orders) every completion replays to the SAME DATE — the unit
``cp_completion`` stores and the objective is scored in. At MINUTE resolution the
residual is one-sided LATE. It is NOT bounded here: the largest value OBSERVED is
+83 min on that harness and +191 min on an OS-blocks + scarce-crew shape, and the
drift check is what derives the number for a given book. It has ONE cause: **this
loop cannot release a successor while its predecessor is still in the chuck.**
``_JobState`` tracks one op at a time, and ``_release_successor`` runs when the
operation is finally placed — so an operation that SPANS A SHIFT BOUNDARY defers
its successor's release from the true release moment to its own completion, and a
single-shift bench that has closed in between waits for the next window. The CP
model has no such restriction: its release is a linear bound on start variables
and can fire mid-operation. Fixing it means letting two ops of one job be in
flight at once, which is a change to this loop's concurrency model, not a tweak.

Two things the genome still does NOT carry, and what happens to each — stated
here because a replay that silently invents an answer is worse than one that
says so:

  * **the calendar.** Under E2 (``cp_hold_across_unmanned_shift``) a task's
    ``breaks``/``idle`` are a BOOKKEEPING SPLIT of its span, not physical time.
    What is workable is read from ``cp_engine.windows`` — the same shift list the
    model was built on — and never off a solved task.
  * **k above its lower bound.** ``cp_overlap_of`` is "at least this many pieces
    had cleared", never "the release this schedule needs" (``rules.add_release``).
    It is read as a piece COUNT and turned into a moment by the exact worked-minute
    walk below, which is the tighter of the two readings.

The bench crew USED to be a third: Rule 1 rosters CNC/VMC only, so ``cp_roster``
names nobody at a manual or inspection station, while ``model._add_modes`` really
does book a capacity-1 operator renewable there. That decision now rides in
``cp_bench_of``. The qualified-pool pick below survives as the FALLBACK for an op
the genome never saw, and it keeps its clash check — deleting it puts one helper
at two benches at the same instant, which the model forbade and which the
shift-wise export shows on the floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from cp_engine import windows
from cp_engine.domain import (DISPATCH, INSPECTION, MACHINING, MANUAL,
                              OUTSOURCED)
from engine.pipeline import KEY_SEP

_EPS = 1e-9
_SECOND = timedelta(seconds=1)

# The kinds that hand pieces over gradually — the same predicate as
# ``rules._overlaps`` and the sibling greedy engine's ``release.overlaps``. The
# model and the decoder must agree about WHICH pairs overlap or they are not
# replaying the same plan; only the MOMENT differs between them, by design.
_INHOUSE = (MACHINING, MANUAL, INSPECTION)

# How far the working calendar is walked. Matches the sibling engine's bound and
# is only ever a SPEED limit: whatever it stops short of is reported through the
# same degrade path as genuinely unplaceable work, never silently lost.
_HORIZON_DAYS = 400

# Consecutive shift windows that may pass with nothing placed, while work is
# still outstanding, before the walk gives up. A single outsourcing block is the
# longest legitimate quiet spell (the real book carries up to 264 h).
_STALL_WINDOWS = 120

_SUPERSEDED = "superseded by an earlier-started row for the same operation"
_CONFLICT = ("two in-progress rows name DIFFERENT machines for one operation "
             "(%s and %s); it runs once, so %s was kept — the older start wins")


class Unschedulable(RuntimeError):
    """NOTHING in this book could be replayed — not one job reached its last step.

    Deliberately narrow, and the same contract the sibling engine's version
    carries: one machine losing its only qualified operator must cost that
    machine's ORDERS their plan, never the whole book's. ``lay_out`` therefore
    DROPS what it could not place and reports it in ``Plan.dropped``; this is
    raised only when that would leave nothing at all.
    """

    def __init__(self, message: str, blocked=()):
        super().__init__(message)
        self.blocked = tuple(blocked)


class PlanStartMissing(ValueError):
    """``plan_start`` was None. A caller bug, not a shop condition — the API
    boundary resolves the nullable "auto: start from today" to a real datetime
    and the pure engine must never see None. TYPED so the seam can contain it as
    a localized ``RuleError`` instead of blanking every per-rule tab."""


@dataclass(frozen=True)
class Placement:
    """One operation, placed. FIELD-FOR-FIELD identical to the sibling greedy
    engine's ``scheduler.Placement`` — job_key, op_seq, op_name, kind, machine,
    qty, start, end, work_min, segments — so the app seam's entry-building logic
    ports across unchanged rather than being written a second way.

    ``segments`` is the per-shift operator record: ((start, end, operator), ...).
    ``work_min`` is the machine time the operation actually consumed; it can be
    less than ``end - start`` when the operation was HELD over a dark shift or a
    weekly off, and/or its end was paced out to its predecessor's.
    """

    job_key: str
    op_seq: int
    op_name: str
    kind: str
    machine: str | None
    qty: int
    start: datetime
    end: datetime
    work_min: float
    segments: tuple = ()          # ((start, end, operator), ...)


@dataclass(frozen=True)
class Plan:
    """``unassigned`` is every job key the genome did not fully cover — an order
    uploaded since the solve, or one whose routing has moved under it. Those jobs
    ARE laid out (on their routing's first machine option); the flag is what the
    staleness banner reads. They are never dropped: work in no plan at all is the
    2026-08-11 director escalation.

    ``unpinned`` is the frozen rows this plan could NOT honour, ``((row, reason),
    ...)``; the accounting closes, so every row in is either an applied pin or a
    row here. ``dropped`` is the jobs no shift in the horizon could place, as
    ``((job_key, op_seq, op_name, machine_options, reason), ...)`` — physically
    unplaceable work, a different thing from ``unassigned``.
    """

    placements: tuple
    completion: dict
    unassigned: tuple = ()
    unpinned: tuple = ()
    dropped: tuple = ()


class _JobState:
    __slots__ = ("job", "idx", "ready", "prev_end", "worked", "on_machine",
                 "released_at", "pinned", "pin_rank")

    def __init__(self, job, plan_start):
        self.job = job
        self.idx = 0
        self.ready = plan_start        # when the CURRENT op may start
        self.prev_end = plan_start     # latest end across every op done so far
        self.worked = 0.0              # worked minutes on the op in progress
        self.on_machine = None         # machine that has CLAIMED the current op
        self.released_at = None        # overlap release moment for the NEXT op
        self.pinned = {}               # op_seq -> machine the part is PHYSICALLY on
        self.pin_rank = {}             # op_seq -> previous-plan resume order


class _MachineState:
    __slots__ = ("job_key", "op_seq", "remaining", "last_key", "segments",
                 "started", "pace_floor", "busy_until", "setup_charged",
                 "pinned_jobs")

    def __init__(self):
        self.job_key = None
        self.op_seq = None
        self.remaining = 0.0
        self.last_key = None           # (item_code, op_seq) of the last job run
        self.segments = []
        self.started = None
        self.pace_floor = None         # this op may not END before its predecessor
        self.busy_until = None         # occupied to here, ACROSS shift boundaries
        self.setup_charged = 0.0       # setup ACTUALLY in the segments (0 if skipped)
        self.pinned_jobs = []          # (rank, seq pos, job key) frozen HERE


@dataclass(frozen=True)
class _Window:
    """One shift, in wall clock. ``index`` is ``windows.Shift.index`` — the SAME
    number ``cp_roster`` is keyed on, which is the only reason a stored roster can
    be read back at all."""

    index: int
    shift: str
    start: datetime
    end: datetime


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def lay_out(jobs, shop, config, plan_start, g: dict, *, frozen=None) -> Plan:
    """Replay a genome against today's book.

    Args:
        jobs: ``cp_engine.domain.Job`` list — today's book, not the solved one.
        shop: ``cp_engine.domain.Shop``.
        config: the app's ``Config`` — shift hours and ``setup_time_min`` only.
            Nothing here re-derives a working window (2026-08-07).
        plan_start: the plan clock. The genome's shift indices are relative to
            THIS moment, so it must be the one the solve was given.
        g: the genome (``cp_engine.genome.KEYS``). Missing keys degrade to the
            fallback path and are flagged, never guessed at silently.
        frozen: in-progress rows. A pin beats the genome; the genome beats a
            fallback.
    """
    if plan_start is None:
        raise PlanStartMissing(
            "plan_start is None — resolve it to a real datetime at the API "
            "boundary before calling the decoder")

    setup_min = float(getattr(config, "setup_time_min", 90.0) or 0.0)
    machine_of = dict(g.get("cp_machine_of") or {})
    roster = dict(g.get("cp_roster") or {})
    overlap_of = dict(g.get("cp_overlap_of") or {})
    ranks = dict(g.get("ranks") or {})
    floors = _floors(g.get("cp_start_of"), plan_start)
    bench_of = dict(g.get("cp_bench_of") or {})

    by_key = {j.key: j for j in jobs}
    order = _order(by_key, ranks)
    state = {key: _JobState(by_key[key], plan_start) for key in order}
    machines = {mid: _MachineState() for mid in sorted(shop.machines)}

    # Order matters: the pins have to be in place before the assignment is read,
    # because a pin OVERRIDES the genome's machine.
    unpinned = _apply_frozen(frozen, state, machines, shop)
    crew = _Crew(shop, roster)
    assigned, unassigned, fallback_ops = _assign(order, state, machine_of,
                                                 crew.rostered_machines)

    placements: list = []
    completion: dict = {}

    stalled = 0
    for window in _windows(plan_start, shop, config):
        if not _outstanding(state):
            break
        cursor = max(window.start, plan_start)
        if cursor >= window.end:
            continue
        before = len(placements)
        placements.extend(_settle_milestones(state, window.end, completion,
                                             floors))
        _run_shift(window, cursor, order, state, machines, shop, crew, assigned,
                   fallback_ops, floors, bench_of, overlap_of, config, setup_min,
                   placements, completion)
        stalled = 0 if len(placements) > before else stalled + 1
        if stalled > _STALL_WINDOWS:
            break

    # DEGRADE AT THE PLACEMENT-FAILURE BOUNDARY, exactly as the sibling engine
    # does. One unstaffable machine must not cost every other order its plan
    # (CLAUDE.md principle 5(a)); an order is either fully planned or absent,
    # never half-drawn on the Gantt.
    unfinished = [key for key in order if state[key].idx < len(state[key].job.ops)]
    blocked = tuple(_blocked_row(state[key], shop, assigned) for key in unfinished)
    if unfinished and len(unfinished) == len(order):
        raise Unschedulable(
            "the CP decoder could not place a single order within the horizon: "
            + _blocked_text(blocked), blocked)
    if unfinished:
        gone = set(unfinished)
        placements = [p for p in placements if p.job_key not in gone]
        for key in gone:
            completion.pop(key, None)

    placements = _pace(placements, completion)
    return Plan(tuple(placements), completion, tuple(unassigned),
                tuple(unpinned), blocked)


# --------------------------------------------------------------------------- #
# Reading the genome — every decision, and nothing else
# --------------------------------------------------------------------------- #

def _order(by_key, ranks) -> list:
    """The job order, from ``ranks``. Ranked jobs first in rank order, then
    anything the genome never saw, in book order (spec §7: "orders CP never saw
    are appended after the ranked work and flagged").

    A job carries several ``(so, item)`` keys when Rule 1 clubbed lines into it,
    and they can rank differently; the EARLIEST wins, because the batch is one
    piece of work and the solver put it where its most urgent member needed it.
    Never a set — the order IS a decision and must be exactly reproducible.
    """
    def rank_of(key):
        job = by_key[key]
        found = [ranks[f"{so}{KEY_SEP}{job.item_code}"] for so in job.so_refs
                 if f"{so}{KEY_SEP}{job.item_code}" in ranks]
        return min(found) if found else None

    positioned = list(enumerate(by_key))
    ranked = [(rank_of(key), pos, key) for pos, key in positioned
              if rank_of(key) is not None]
    unranked = [(pos, key) for pos, key in positioned if rank_of(key) is None]
    return [key for _rank, _pos, key in sorted(ranked)] + \
           [key for _pos, key in unranked]


def _floors(raw, plan_start) -> dict:
    """``(job key, op seq) -> the solved START``, as a datetime.

    Stored ABSOLUTE (ISO strings) rather than as minutes from the plan start.
    The reason is that a floor is only meaningful against a fixed wall clock: an
    absolute one is self-describing, so a replay given a DIFFERENT ``plan_start``
    from the solve gets floors that are visibly in the past and inert, rather
    than floors that silently re-anchor to the new clock and claim to pin a plan
    they no longer describe.

    It is NOT a defence against the plan clock moving, and an earlier version of
    this comment wrongly said it was. Nothing here survives that: ``cp_roster``
    is keyed on a shift INDEX counted from ``plan_start``, ``_windows`` rebuilds
    the calendar from ``plan_start``, and the clamp below lifts every past floor
    up to it — so a replay on a later clock slides and can re-order jobs
    (measured: solve at 12-08 08:00, replay at 13-08 moved every completion a day
    and swapped two jobs). That is why ``lay_out`` requires the plan_start the
    solve was given; a genome under a moved clock is STALE, and the staleness
    banner, not this key, is what must catch it.

    Datetimes are accepted too, so an in-process hand-off that never went through
    JSON works the same way.
    """
    out: dict = {}
    for key, value in (raw or {}).items():
        when = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value))
        out[key] = max(when, plan_start)
    return out


def _floor_for(js, op, floors):
    """The solved start this op may not begin before, or None.

    A FROZEN op has none: a pin beats the genome, the part is physically in the
    chuck, and holding it back to a start the solver computed for a book that has
    since moved would idle a running machine.
    """
    if op.seq in js.pinned:
        return None
    return floors.get((js.job.key, op.seq))


def _ready_at(js, op, floors):
    """When this op may start: its routing/overlap readiness, floored by the
    solved start. This is THE one definition — every place that asks "can it go
    yet" goes through it, or the floor would hold in one branch and not another."""
    ready = js.ready
    floor = _floor_for(js, op, floors)
    return ready if floor is None else max(ready, floor)


def _assign(order, state, machine_of, rostered_machines) -> tuple:
    """``(job key, op seq) -> machine``, the job keys the genome did not fully
    cover, and the (job, op) pairs it did not cover.

    The third value is per-OP on purpose. Keyed on the MACHINE instead, one new
    order landing on CNC1 switched pool staffing on for EVERY dark CNC1 shift for
    the whole horizon — including the shifts carrying the genome's own solved
    work. Measured: a solved order whose replay completed 14-08 15:10 alone
    completed 13-08 02:10 once an unrelated 30-piece order was added, running its
    tail through a night shift the solver had deliberately left dark. That is
    EARLY completion drift on solved work, the one direction that must not
    happen, and no drift harness that replays only the SOLVED book can see it.

    THE CONFLICT ORDER, and it is load-bearing: **a frozen pin beats the genome;
    the genome beats a fallback.** A pin is where the part physically IS. A
    genome entry the current routing no longer lists is stale, not authoritative,
    so it falls through — but the step still runs and its job is flagged. Nothing
    is ever dropped for want of an assignment.

    The fallback prefers an option the genome actually MANS. ``options[0]`` alone
    is a trap: measured on a real solve of a ``CNC1/CNC4`` routing where the
    genome rostered CNC1 in 2 shifts of 33 and CNC4 in 31, a new order took CNC1
    and sat idle **19 days** waiting for its next rostered shift while CNC4 stood
    staffed. The brief's own wording was "its routing's first machine option AND
    any qualified rostered operator"; this is the second half of it.
    """
    assigned: dict = {}
    unassigned: list = []
    fallback_ops: set = set()
    for key in order:
        js = state[key]
        stale = False
        for op in js.job.ops:
            if not op.machine_options:
                continue          # OS / DISPATCH / a milestone: no machine to assign
            pin = js.pinned.get(op.seq)
            if pin is not None:
                assigned[(key, op.seq)] = pin
                continue
            mid = machine_of.get((key, op.seq))
            if mid in op.machine_options:
                assigned[(key, op.seq)] = mid
                continue
            mid = next((m for m in op.machine_options
                        if m in rostered_machines), op.machine_options[0])
            assigned[(key, op.seq)] = mid
            fallback_ops.add((key, op.seq))
            stale = True
        if stale:
            unassigned.append(key)
    return assigned, tuple(unassigned), frozenset(fallback_ops)


class _Crew:
    """Who is at a machine, in a given shift — read from the genome where the
    genome has an answer, and from the Settings table where it structurally
    cannot.

    ``cp_roster`` covers CNC/VMC only. So a machine that appears in it ANYWHERE
    is a machine the solve rostered, and a shift it is missing from is a shift
    the solver deliberately left dark — under E2 the part simply waits in the
    chuck, which is the whole point of that encoding. A machine that appears in
    it NOWHERE is either a bench (never rostered) or a machine added since the
    solve; both are staffed from the qualified pool.
    """

    def __init__(self, shop, roster):
        self.shop = shop
        self.roster = roster
        self.rostered_machines = {mid for mid, _shift in roster}
        self.by_name = {o.name: o for o in shop.operators}

    def available(self, name, window) -> bool:
        operator = self.by_name.get(name)
        if operator is None:
            return False
        if windows.operator_shift(operator) != window.shift:
            return False
        return not any(s < window.end and window.start < e
                       for s, e in self.shop.absent.get(name, ()))

    def staff(self, mid, window, may_pool: bool) -> tuple:
        """``(the one rostered person or None, the pool, is this an ESCAPE)``.

        Exactly one of four outcomes, and which one is a rule, not a preference:

        * the genome names someone who can be there -> that person, no pool. The
          decoder decides nothing.
        * the genome names someone who CANNOT be there (an admin edited their
          shift, or they are away since the solve) -> the pool. The plan wants
          this machine manned; leaving it dark because the named man moved would
          take the machine out of the replay entirely, for the whole horizon.
        * the genome never rosters this machine at all — a bench, or a machine
          added since the solve -> the pool, unrestricted.
        * the genome rosters this machine SOMEWHERE and named nobody here. It was
          left dark on purpose (under E2 the part waits in the chuck, which is
          the point of that encoding), so it stays dark UNLESS it is carrying an
          op the genome never assigned. Then the pool staffs it as an **ESCAPE**,
          and the caller must confine that shift to those ops alone — the
          capacity exists for the new order, not for the solved book.
        """
        named = self.roster.get((mid, window.index))
        if named is not None and self.available(named, window):
            return named, (), False
        if named is None and mid in self.rostered_machines:
            if not may_pool:
                return None, (), False
            return None, self._pool(mid, window), True
        return None, self._pool(mid, window), False

    def _pool(self, mid, window) -> tuple:
        """Qualified, on this shift, present — name-sorted so the answer can
        never depend on the operator table's order."""
        return tuple(o.name for o in sorted(self.shop.operators,
                                            key=lambda o: o.name)
                     if mid in (getattr(o, "machines", None) or ())
                     and self.available(o.name, window))


def _released_pieces(job, op, overlap_of, config) -> int:
    """How many whole pieces must clear ``op`` before its successor may start.

    From the genome. A job the genome never saw falls back to the app's own
    ``overlap_percent`` knob and the app's own rounding rule (ceil — releasing on
    5.6 pieces would start a process on a piece that does not exist), which is
    what a non-CP plan of that order would have done. Clamped into
    ``1..qty_for(op)`` either way: ``rules.add_release`` sizes k on the smallest
    remainder any step of the job owes, so a larger number here would be counting
    in a quantity the model never used.
    """
    qty = job.qty_for(op.seq)
    if qty <= 0:
        return 0
    k = overlap_of.get(job.key)
    if k is None:
        percent = float(getattr(config, "overlap_percent", 50) or 0) / 100.0
        k = math.ceil(min(1.0, max(0.0, percent)) * qty)
    return max(1, min(qty, int(k)))


# --------------------------------------------------------------------------- #
# The working calendar — ONE definition, shared with the model
# --------------------------------------------------------------------------- #

def _windows(plan_start, shop, config):
    """Every working shift, as wall clock, carrying the index the genome's roster
    is keyed on.

    Built from ``windows.build_shifts`` — the same function ``model.build`` was
    given — so the decoder and the model can never disagree about when the shop
    is open. Under E2 a solved task's ``breaks``/``idle`` are a bookkeeping split
    of its span rather than physical time, so they are not read here at all.
    """
    for shift in windows.build_shifts(plan_start, shop.calendar, config,
                                      _HORIZON_DAYS):
        yield _Window(shift.index, shift.shift,
                      plan_start + timedelta(minutes=shift.start),
                      plan_start + timedelta(minutes=shift.end))


def _machine_runs(machine, shift: str) -> bool:
    """A two-shift machine (Available Hrs/Day >= 12) runs both shifts; a
    single-shift station runs the FIRST shift only — the full 08:00-19:00 window,
    not the legacy 09:00-18:00 manual one (2026-08-07: that discrepancy hid 9,470
    minutes of real planned work from four reporting features)."""
    if shift == windows.FIRST:
        return True
    return bool(machine.is_two_shift())


# --------------------------------------------------------------------------- #
# Work minutes
# --------------------------------------------------------------------------- #

def _setup_for(js, op, ms, mid, shop, setup_min: float) -> float:
    """The setup this operation actually pays on this machine.

    Keyed on the MACHINE, never on ``op.kind``. The model charges setup per MODE
    (``mid in shop.machining_ids``, ``model.build``), and keying it on the step's
    kind is wrong in BOTH directions — an ``MD1/CNC1`` step is manual-kind and
    still changes a fixture on a CNC, a ``CNC1/MD1`` step is machining-kind and
    changes nothing at a bench. That confusion produced two separate defect
    rounds in the model layer; it is not repeated here.

    None when the operation is RESUMED (frozen — the part is in the chuck and the
    fixture is on), and none when the machine is already set up for the same
    (item, process): Rule 4's credit, the same pairing ``rules.add_setup_credit``
    grants a setup-free mode to.
    """
    if op.seq in js.pinned:
        return 0.0
    if mid not in shop.machining_ids:
        return 0.0
    if ms.last_key == (js.job.item_code, op.seq):
        return 0.0
    return setup_min


def _overlaps(prev, nxt) -> bool:
    """May ``nxt`` start before ``prev`` has finished? The same predicate as
    ``rules._overlaps``: both steps in-house, and the predecessor actually
    cutting. A step with no cycle time produces nothing gradually, so its
    successor waits for it to complete (RULES.md:127)."""
    if prev.kind not in _INHOUSE or nxt.kind not in _INHOUSE:
        return False
    return prev.cycle_min > 0.0


# --------------------------------------------------------------------------- #
# Milestones — no machine, no crew
# --------------------------------------------------------------------------- #

def _settle_milestones(state, now, completion, floors) -> list:
    """Place every OS / DISPATCH / nothing-left-to-make step that is ready by
    ``now``.

    DISPATCH is placed at the latest end across the WHOLE batch, never at its
    immediate predecessor's release point — an order is dispatched only once
    every piece has cleared every process (RULES.md:305). ``js.prev_end`` is
    exactly that running maximum.
    """
    out = []
    progressed = True
    while progressed:
        progressed = False
        for js in state.values():
            op = _current_op(js)
            if op is None:
                continue
            qty = js.job.qty_for(op.seq)
            if op.kind == OUTSOURCED:
                # Fully sequential both sides: nothing overlaps INTO a vendor
                # block and js.ready is its predecessor's end. An OS step IS a
                # task in the model (a mode on the OS pool), so it carries a
                # solved start and is floored like any other.
                start = _ready_at(js, op, floors)
                if start > now:
                    continue
                lead = float(op.cycle_min or 0.0)
                end = start + timedelta(minutes=lead)
                out.append(Placement(js.job.key, op.seq, op.name, OUTSOURCED,
                                     None, int(max(qty, 0)), start, end, lead,
                                     ()))
                _advance(js, end, completion)
            elif op.kind == DISPATCH:
                # ``prev_end`` is the running maximum, ``ready`` the release
                # moment. They are provably equal here — ``_release_successor``
                # only sets a release when ``_overlaps(op, nxt)``, and a DISPATCH
                # successor is never in ``_INHOUSE`` — so this is belt-and-braces
                # (measured: mutating it to ``js.ready`` kills no test). It is
                # kept because it is the direct statement of RULES.md:305, and
                # the equality stops holding the moment anything releases past a
                # milestone.
                at = js.prev_end
                if at > now:
                    continue
                out.append(Placement(js.job.key, op.seq, op.name, DISPATCH, None,
                                     int(max(qty, 0)), at, at, 0.0, ()))
                _advance(js, at, completion)
            elif qty <= 0 or not op.machine_options:
                # Nothing left to make at this step (a re-plan's already-finished
                # process): a visible zero-duration milestone, never a silent skip.
                at = max(js.ready, js.prev_end)
                if at > now:
                    continue
                out.append(Placement(js.job.key, op.seq, op.name, op.kind, None,
                                     0, at, at, 0.0, ()))
                _advance(js, at, completion)
            else:
                continue                  # needs a machine; the shift loop has it
            progressed = True
    return out


def _advance(js, end_at, completion):
    """Record that the current op finished at ``end_at`` and open the next one."""
    js.prev_end = max(js.prev_end, end_at)
    js.idx += 1
    js.worked = 0.0
    js.on_machine = None
    if _current_op(js) is None:
        completion[js.job.key] = js.prev_end
    else:
        # THE overlap rule, in one line: the next step opens at the moment whole
        # pieces cleared this one, and only falls back to "when it finished" when
        # nothing pipelines (OS, a milestone, a zero-cycle step, k == qty).
        js.ready = js.released_at if js.released_at is not None else js.prev_end
    js.released_at = None


def _release_successor(js, op, segments, overlap_of, config, setup_charged):
    """Open the next operation once ``k`` whole pieces have cleared this one.

    ``setup_charged`` is the setup this machine ACTUALLY put in the segments, not
    the nominal ``config.setup_time_min``: a repeat of the same (item, process)
    is already set up and pays none, and so does a resumed frozen op. This is the
    same expression ``rules._setup_charged`` reads off the model — one setup, two
    layers, never two definitions.
    """
    js.released_at = None
    if js.idx + 1 >= len(js.job.ops):
        return
    nxt = js.job.ops[js.idx + 1]
    if not _overlaps(op, nxt):
        return
    pieces = _released_pieces(js.job, op, overlap_of, config)
    need = setup_charged + pieces * float(op.cycle_min or 0.0)
    js.released_at = _release_moment(segments, need)


def _release_moment(segments, need: float):
    """The wall-clock instant at which the machine had done ``need`` minutes of
    WORK on this operation.

    Worked minutes, not elapsed — this is the exact half of the pair. An
    operation held overnight, or over the weekly off, cuts nothing in the gap;
    measuring from the operation's start would release pieces in the middle of a
    dark shop, and the CP model's own bounds are wall-clock precisely because a
    linear constraint cannot express this walk (spec §5.3).
    """
    done = 0.0
    for seg_start, seg_end, _who in segments:
        span = (seg_end - seg_start).total_seconds() / 60.0
        if done + span >= need - _EPS:
            return seg_start + timedelta(minutes=max(0.0, need - done))
        done += span
    return segments[-1][1] if segments else None


# --------------------------------------------------------------------------- #
# One shift
# --------------------------------------------------------------------------- #

def _pending_escape_machines(order, state, assigned, fallback_ops) -> set:
    """Machines still owing an op the genome never assigned.

    Per-OP and re-derived every shift, so the escape below closes again the
    moment the new work is done — a machine does not stay pool-staffed for the
    rest of the horizon because one order once needed it.
    """
    if not fallback_ops:
        return set()
    out = set()
    for key in order:
        js = state[key]
        for op in js.job.ops[js.idx:]:
            if (key, op.seq) in fallback_ops:
                mid = assigned.get((key, op.seq))
                if mid is not None:
                    out.add(mid)
    return out


def _first_work_moment(mid, ms, order, state, assigned, floors, cursor, window,
                       only):
    """``(earliest moment this machine could act in this window, the genome rank
    of the job it would act on)``, or ``None`` when it has nothing to run.

    The SAME walk ``_next_on_machine`` and ``_earliest_ready`` do, asked one
    question earlier: it is what decides whether reserving a person for this
    machine is justified at all. Deliberately not a second definition of "has
    work" — a staffing rule that disagreed with the dispatch rule would book
    people onto machines that then stand idle, which is the exact defect it
    exists to stop.

    The second element is the tie-break, and it is READ, NEVER DECIDED: among
    machines whose work is ready at the same instant the person goes to the one
    carrying the job the genome ranked first. Machine id is only the final
    determinism tie-break, never a reason — an alphabetical pick handed the
    shop's one flexible helper to CNC1 while CNC4's ready-now job went unplanned.

    It reads CURRENT ops only, so work released into this shift by another
    machine's overlap mid-window is not seen and the machine waits for the next
    window. That is a one-shift delay, it is the conservative direction, and it
    can never drop anything: the op is still current at the next window's start,
    when it IS seen.

    **WHICH MACHINES THAT DELAY CAN TOUCH, stated exactly** — an earlier version
    of this said "pool-staffed machines only (a genome-rostered machine is manned
    regardless of demand)", and that is false for one of ``_Crew.staff``'s four
    branches. A machine IS manned regardless of demand only while the genome's
    named operator is available; if that person's shift was edited or they are
    away since the solve, ``staff`` returns ``(None, pool, False)`` and the
    machine is staffed from the pool like any other — so it is subject to this
    question, and to this delay, exactly like an unrostered bench. Rostered is
    therefore not a safe-list: the demand test is what decides, every time.
    """
    if ms.job_key is not None:
        # A part physically in the chuck: the machine has work by construction
        # (``ms.job_key`` is cleared the moment the op finishes).
        if only is not None and (ms.job_key, ms.op_seq) not in only:
            return None
        when = max(cursor, ms.busy_until or cursor)
        if when >= window.end:
            return None
        return when, order.index(ms.job_key)
    best = None
    for rank, key in enumerate(order):
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or assigned.get((key, op.seq)) != mid:
            continue
        if only is not None and (key, op.seq) not in only:
            continue
        if js.job.qty_for(op.seq) <= 0:
            continue
        when = max(cursor, _ready_at(js, op, floors))
        if when >= window.end:
            continue
        if best is None or (when, rank) < best:
            best = (when, rank)
    return best


def _run_shift(window, cursor, order, state, machines, shop, crew, assigned,
               fallback_ops, floors, bench_of, overlap_of, config, setup_min,
               placements, completion):
    """Advance every staffed machine through one shift.

    Within the shift the machine that can act EARLIEST moves, rather than each
    machine in turn to the end of the window: that is what lets a successor
    released by overlap at 10:50 actually start at 10:50 on a machine idle since
    08:00, instead of losing the shift because its feeder happened to be
    processed later in a fixed machine order.
    """
    live = {mid for (_key, _seq), mid in assigned.items()}
    escapable = _pending_escape_machines(order, state, assigned, fallback_ops)
    manned: dict = {}
    benches: dict = {}
    booked: dict = {}
    pooled: list = []
    restricted: set = set()
    for mid in sorted(shop.machines):
        if mid not in live and machines[mid].job_key is None:
            continue                      # nothing is ever assigned here
        if not _machine_runs(shop.machines[mid], window.shift):
            continue
        who, pool, escaped = crew.staff(mid, window, mid in escapable)
        if escaped:
            # The capacity exists for the op the genome never assigned, and for
            # nothing else. Letting the solved book use it moves solved orders
            # EARLIER, which is the one direction that must not happen.
            restricted.add(mid)
        if who is not None:
            manned[mid] = who
        elif pool:
            pooled.append((mid, pool))

    # Reserve the genome's own people FIRST. A rostered person is on that machine
    # for the whole shift and cannot also be at a bench — the same reservation
    # ``rules._reserve_rostered_operators`` makes in the model (the live
    # 2026-08-07 case ran manual stations AND CNC4). Doing this before the pool
    # picks below is what stops a pool pick stealing a rostered man.
    for name in manned.values():
        booked.setdefault(name, []).append((window.start, window.end))

    # A pool-staffed MACHINING machine binds ONE person for the WHOLE shift, the
    # same way the roster binds the genome's own — staffing a CNC per operation
    # like a bench publishes a plan in which one man runs a CNC and then walks to
    # a bench in the same shift, which ``operator_split_violations`` flags and
    # spec §8 requires to be zero.
    #
    # It binds that person ONLY IF THE MACHINE HAS SOMETHING TO RUN in this
    # window, and where two such machines want the same person the one whose
    # work is ready SOONEST — then the one carrying the genome's higher-ranked
    # job — picks first. Both clauses are load-bearing and both were learned the
    # hard way.
    #
    # An unconditional reservation is PERMANENT: ``live`` is derived once from
    # ``assigned`` and is never pruned as jobs finish, so a CNC that emptied at
    # 10:00 on day one went on eating the shop's only flexible helper for the
    # rest of the 400-day horizon — measured at 5 of 7 real orders DROPPED, the
    # 2026-08-11 director-escalation shape and the very thing the escape exists
    # to prevent. And picking in machine-id order handed that helper to an idle
    # CNC1 while CNC4's ready-now job was dropped: the decoder must not settle a
    # contest by an accident of naming when the genome has an opinion. Gating
    # here rather than pruning ``live`` keeps ONE definition of "has work" — the
    # same walk ``_next_on_machine`` does.
    machining_pool, bench_pool = [], []
    for mid, pool in pooled:
        if mid not in shop.machining_ids:
            bench_pool.append((mid, pool))
            continue
        when = _first_work_moment(mid, machines[mid], order, state, assigned,
                                  floors, cursor, window,
                                  fallback_ops if mid in restricted else None)
        if when is not None:
            machining_pool.append((when[0], when[1], mid, pool))

    for _when, _rank, mid, pool in sorted(machining_pool):
        pick = next((name for name in pool
                     if not any(s < window.end and window.start < e
                                for s, e in booked.get(name, ()))), None)
        if pick is None:
            continue                  # nobody free all shift; the machine is dark
        manned[mid] = pick
        booked.setdefault(pick, []).append((window.start, window.end))

    for mid, pool in bench_pool:
        benches[mid] = pool
        for name in pool:
            booked.setdefault(name, [])

    if not manned and not benches:
        return                      # every machine dark; anything in a chuck HOLDS

    free_at = {}
    for mid in list(manned) + list(benches):
        busy = machines[mid].busy_until
        free_at[mid] = max(cursor, busy) if busy else cursor

    while True:
        best = None
        for mid in sorted(free_at):
            now = free_at[mid]
            if now >= window.end - _SECOND:
                continue
            only = fallback_ops if mid in restricted else None
            ms = machines[mid]
            if ms.job_key is not None:
                if only is not None and (ms.job_key, ms.op_seq) not in only:
                    continue        # solved work in the chuck; this shift is not its
                start = now                      # the part is already in the chuck
            elif _next_on_machine(mid, ms, order, state, assigned,
                                  floors, now, only) is not None:
                start = now
            else:
                # Nothing startable yet — but work released later in THIS shift
                # is still this shift's work; wait for it rather than going home.
                start = _earliest_ready(mid, order, state, assigned, floors, now,
                                        window.end, only)
                if start is None:
                    continue
            if best is None or (start, mid) < best:
                best = (start, mid)
        if best is None:
            return
        start, mid = best
        only = fallback_ops if mid in restricted else None
        if mid in benches:
            who, retry_at = _bench_operator(mid, machines[mid], order, state,
                                            assigned, floors, bench_of, shop,
                                            window, start, setup_min,
                                            benches[mid], booked, only)
            if who is None:
                # Every qualified helper is on another bench across this run. The
                # bench waits for one — it does not take half a person, and it
                # does not go home for the shift.
                free_at[mid] = min(retry_at, window.end)
                continue
        else:
            who = manned[mid]
        free_at[mid] = _work(mid, machines[mid], order, state, assigned, floors,
                             shop, window, start, who, overlap_of, config,
                             setup_min, placements, completion, only)


def _bench_operator(mid, ms, order, state, assigned, floors, bench_of, shop,
                    window, start, setup_min, pool, booked, only=None):
    """Staff a manual / inspection station.

    Returns ``(operator_name, None)`` and books them, or ``(None, retry_at)``
    when every qualified helper is occupied across the run.

    **The genome answers this whenever it can.** ``cp_bench_of`` carries who the
    solver booked, which ``cp_roster`` structurally cannot (Rule 1 rosters
    CNC/VMC only). The named person is tried FIRST and, being the solver's own
    choice, is free at that moment on the book that was solved.

    The pool pick is the FALLBACK, for an op the genome never saw or whose named
    helper has left the Settings table. It resolves by INTERVAL EXCLUSION: ties
    by name (``pool`` is name-sorted), never the same person at two stations at
    the same instant — that is what the model forbade, and the shift-wise sheet
    shows it on the floor.

    The person is booked for the WHOLE projected run, not per minute: Rule 2
    keeps an operation whole, so a helper who starts a bench stays on it until
    the operation finishes or the shift ends.
    """
    peek = _bench_run_minutes(mid, ms, order, state, assigned, floors, shop,
                              window, start, setup_min, only)
    if peek is None:
        return None, window.end                   # nothing here to staff
    minutes, task_key = peek
    finish = start + timedelta(minutes=minutes)
    named = bench_of.get(task_key)
    if named is not None and named in pool:
        pool = (named,) + tuple(n for n in pool if n != named)
    retry_at = None
    for name in pool:
        clash = None
        for busy_start, busy_end in booked.get(name, ()):
            if busy_start < finish and start < busy_end:
                clash = busy_end if clash is None else max(clash, busy_end)
        if clash is None:
            booked.setdefault(name, []).append((start, finish))
            return name, None
        if retry_at is None or clash < retry_at:
            retry_at = clash
    return None, (retry_at if retry_at is not None else window.end)


def _bench_run_minutes(mid, ms, order, state, assigned, floors, shop, window,
                       start, setup_min, only=None):
    """``(minutes this bench would hold a person from ``start``, the (job, op)
    it would run)``, or None if it has nothing to run.

    A pure peek: it mirrors exactly what ``_work`` is about to compute, without
    touching a thing, because a bench with no free helper must not claim the job
    at all. It hands back the task key as well, so the caller can ask the genome
    who the solver put on THAT op rather than on the bench in general.
    """
    if ms.job_key is not None:
        remaining = ms.remaining                  # part already on the bench
        task_key = (ms.job_key, ms.op_seq)
    else:
        picked = _next_on_machine(mid, ms, order, state, assigned, floors,
                                  start, only)
        if picked is None:
            return None
        js, op = picked
        setup = _setup_for(js, op, ms, mid, shop, setup_min)
        remaining = setup + js.job.qty_for(op.seq) * float(op.cycle_min or 0.0)
        task_key = (js.job.key, op.seq)
    return (min(max(remaining, 0.0),
                (window.end - start).total_seconds() / 60.0), task_key)


def _next_on_machine(mid, ms, order, state, assigned, floors, now, only=None):
    """The next job this machine runs. NOT a choice — a lookup.

    A part the machine is already physically holding goes first, in previous-plan
    start order (the shop cannot start a new part in a chuck that already holds a
    half-finished one); everything else follows the genome's job order. The claim
    (``js.on_machine``) is what stops one batch being built twice; here it can
    only ever be this machine, because the assignment is fixed before the walk
    starts, but it still guards a job from being picked up twice within a shift.
    """
    for _rank, _pos, key in ms.pinned_jobs:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or js.pinned.get(op.seq) != mid:
            continue          # that part has moved on; this pin is spent
        if only is not None and (key, op.seq) not in only:
            continue
        if _ready_at(js, op, floors) > now or js.job.qty_for(op.seq) <= 0:
            continue
        return js, op
    for key in order:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or assigned.get((key, op.seq)) != mid:
            continue
        if only is not None and (key, op.seq) not in only:
            continue          # an ESCAPE-staffed shift runs only what it was opened for
        if _ready_at(js, op, floors) > now or js.job.qty_for(op.seq) <= 0:
            continue
        return js, op
    return None


def _earliest_ready(mid, order, state, assigned, floors, after, before,
                    only=None):
    """The first moment strictly after ``after`` and before ``before`` at which
    some unclaimed job could start on this machine."""
    best = None
    for key in order:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or assigned.get((key, op.seq)) != mid:
            continue
        if only is not None and (key, op.seq) not in only:
            continue
        if js.job.qty_for(op.seq) <= 0:
            continue
        ready = _ready_at(js, op, floors)
        if ready <= after or ready >= before:
            continue
        if best is None or ready < best:
            best = ready
    return best


def _work(mid, ms, order, state, assigned, floors, shop, window, start,
          operator, overlap_of, config, setup_min, placements, completion,
          only=None):
    """Advance one machine from ``start``. Returns when it is next free.

    The machine claims a job and does not let go until the operation is finished,
    so no other job can be squeezed into it — Rule 2, and the reason this returns
    a free time rather than a placement per shift.
    """
    if ms.job_key is None:
        picked = _next_on_machine(mid, ms, order, state, assigned, floors,
                                  start, only)
        if picked is None:
            return window.end                     # nothing to do; park it
        js, op = picked
        setup = _setup_for(js, op, ms, mid, shop, setup_min)
        ms.job_key, ms.op_seq = js.job.key, op.seq
        ms.remaining = setup + js.job.qty_for(op.seq) * float(op.cycle_min or 0.0)
        ms.setup_charged = setup
        ms.segments, ms.started = [], None
        # This step may not END before the steps feeding it did: it cannot finish
        # pieces its predecessor has not delivered. Captured at claim time, when
        # the predecessor is by construction already finished.
        ms.pace_floor = js.prev_end
        js.on_machine = mid
        js.worked = 0.0

    js = state[ms.job_key]
    op = js.job.ops[js.idx]

    if ms.remaining > _EPS:
        take = min(ms.remaining, (window.end - start).total_seconds() / 60.0)
        if take <= _EPS:
            return window.end
        seg_end = start + timedelta(minutes=take)
        ms.segments.append((start, seg_end, operator))
        if ms.started is None:
            ms.started = start
        ms.remaining -= take
        js.worked += take
        if ms.remaining > _EPS:
            return seg_end                        # still in the chuck next step
    else:
        # A step with a machine but no work at all (zero cycle time, no setup): a
        # zero-length segment, so the chain still advances and it stays visible.
        # The person IS standing at the bench, and freeze pins machine AND
        # operator off this record.
        seg_end = start
        ms.segments.append((start, seg_end, operator))
        if ms.started is None:
            ms.started = start

    end = max(seg_end, ms.pace_floor) if ms.pace_floor else seg_end
    work = sum((e - s).total_seconds() / 60.0 for s, e, _w in ms.segments)
    placements.append(Placement(js.job.key, op.seq, op.name, op.kind, mid,
                                int(js.job.qty_for(op.seq)), ms.started, end,
                                work, tuple(ms.segments)))
    ms.last_key = (js.job.item_code, op.seq)
    ms.busy_until = end
    _release_successor(js, op, ms.segments, overlap_of, config, ms.setup_charged)
    ms.job_key = ms.op_seq = ms.pace_floor = None
    ms.segments, ms.started, ms.setup_charged = [], None, 0.0
    _advance(js, end, completion)
    placements.extend(_settle_milestones(state, window.end, completion, floors))
    return end


# --------------------------------------------------------------------------- #
# Finishing
# --------------------------------------------------------------------------- #

def _pace(placements, completion) -> list:
    """A step's END is never before an earlier step's END (RULES.md:132), and the
    job's completion is the latest end it has.

    The real work is done at claim time (``_MachineState.pace_floor``), which
    extends the machine's OCCUPANCY with the operation so nothing is dropped into
    the paced tail. This is the belt-and-braces sweep that makes the invariant
    true of the PUBLISHED plan whatever produced it.

    Its redundancy is MEASURED, not assumed: removing this sweep alone kills no
    test, and removing ``pace_floor`` alone kills none either — but removing BOTH
    fails ``test_a_fast_step_never_finishes_before_the_step_that_feeds_it``. Only
    ``pace_floor`` holds the machine, so it is the load-bearing half; this one is
    kept because a later layer that PRE-PLACES operations (frozen work) can
    produce an order this walk has to repair.
    """
    by_job: dict = {}
    for p in placements:
        by_job.setdefault(p.job_key, []).append(p)
    out = []
    for key, items in by_job.items():
        items.sort(key=lambda p: p.op_seq)
        floor = None
        latest = None
        for p in items:
            if floor is not None and p.end < floor:
                # A zero-duration milestone must stay zero-duration: move both ends.
                start = floor if p.start == p.end else p.start
                p = Placement(p.job_key, p.op_seq, p.op_name, p.kind, p.machine,
                              p.qty, start, floor, p.work_min, p.segments)
            floor = p.end if floor is None else max(floor, p.end)
            latest = p.end if latest is None else max(latest, p.end)
            out.append(p)
        if latest is not None:
            completion[key] = latest
    out.sort(key=lambda p: (p.start, p.job_key, p.op_seq))
    return out


def _outstanding(state) -> bool:
    return any(js.idx < len(js.job.ops) for js in state.values())


def _current_op(js):
    return js.job.ops[js.idx] if js.idx < len(js.job.ops) else None


def _blocked_row(js, shop, assigned):
    op = js.job.ops[js.idx]
    mid = assigned.get((js.job.key, op.seq))
    options = (mid,) if mid else tuple(op.machine_options)
    return (js.job.key, op.seq, op.name, options, _blocked_reason(options, shop))


def _blocked_reason(options, shop) -> str:
    """WHY this step could not be placed — CHECKED against Settings, never
    assumed. A report may not attribute a cause it did not check (2026-08-09), so
    when the machines really are staffable the honest answer is that the cause is
    unexplained, and it says so."""
    if not options:
        return "its routing names no machine"
    unstaffed = [m for m in options if not _staffable(m, shop)]
    if len(unstaffed) < len(options):
        return ("the horizon ran out with this step still outstanding; its "
                "machines ARE staffable in Settings, so the cause is not a "
                "missing qualification")
    missing = [m for m in options if m not in shop.machines]
    if len(missing) == len(options):
        return ("no operator in Settings is qualified for it, and it is not in "
                "the Machine master yet")
    return "no operator in Settings is qualified for it"


def _blocked_text(blocked, limit=5) -> str:
    return "; ".join(f"{key} step {seq} '{name}' on "
                     f"{'/'.join(options) or '(no machine)'} ({why})"
                     for key, seq, name, options, why in blocked[:limit])


def _staffable(mid, shop) -> bool:
    """Could ANYBODY in Settings man this machine, on a shift it runs?

    Qualification is exactly the machine list the admin set (2026-08-07).
    Absences are deliberately NOT consulted: they are dated windows, not a
    statement that the machine has lost its crew.
    """
    machine = shop.machines.get(mid)
    for operator in shop.operators:
        if mid not in (getattr(operator, "machines", None) or ()):
            continue
        if machine is None:
            return True
        if _machine_runs(machine, windows.operator_shift(operator)):
            return True
    return False


# --------------------------------------------------------------------------- #
# Reading a frozen row
#
# Duplicated from the sibling greedy engine's ``scheduler`` rather than imported,
# on the same grounds ``cp_engine.domain`` duplicates the domain model: this
# package stands alone and must not inherit that engine's lifecycle. The app seam
# (a later task) must import these from ONE place — never keep a third copy.
# --------------------------------------------------------------------------- #

def row_value(row, *names):
    """One value out of a frozen row, whatever it is spelled and whatever type it
    is. ``engine.freeze.compute_frozen_set`` emits ``machine`` and an ISO STRING
    ``prev_start``; an adapter renames some of that to ``machine_id`` /
    ``order_key``. A pin that silently does nothing because of a key name is the
    worst failure available here — the plan looks fine and the part is on the
    wrong machine — so both spellings are read. Blank counts as absent, so an
    empty ``machine`` falls through rather than pinning to ""."""
    for name in names:
        value = (row.get(name) if hasattr(row, "get")
                 else getattr(row, name, None))
        if value is not None and value != "":
            return value
    return None


def prev_start_key(row):
    """A sortable, type-stable previous-plan start. Datetimes and ISO strings both
    normalise to an ISO string, which sorts chronologically; a missing one sorts
    last rather than blowing up a comparison."""
    value = row_value(row, "prev_start", "start")
    if isinstance(value, datetime):
        return (0, value.isoformat())
    text = str(value).strip() if value is not None else ""
    return (0, text) if text else (1, "")


def pin_rank(row, mid):
    """Which of several rows for ONE (job, op) becomes the pin: the earliest
    previous-plan start, then machine, operator and SO number, so the answer can
    never depend on the order the rows arrive in."""
    return (prev_start_key(row), str(mid),
            str(row_value(row, "operator") or ""),
            str(row_value(row, "so_no") or ""))


def _supersede_reason(kept_mid: str, dropped_mid: str) -> str:
    """Why a second row for one (job, op) was not applied. CHECKED, not assumed:
    same machine -> a duplicate; different machine -> a real conflict, and
    calling that "superseded" would attribute a cause nobody checked."""
    if str(kept_mid) == str(dropped_mid):
        return _SUPERSEDED
    return _CONFLICT % (kept_mid, dropped_mid, kept_mid)


def _apply_frozen(frozen, state, machines, shop):
    """Pin every in-progress operation to the machine it is physically on, and
    return ``[(row, reason)]`` for every row whose pin could NOT be applied.

    THE ACCOUNTING CLOSES: each row that arrives is either the applied pin for its
    (job, op) or it is in the returned list. A silently dropped constraint is a
    named recurring defect class here.

    A frozen row pins WHERE and WHEN, never HOW MUCH. ``remaining_qty`` is
    deliberately NOT read: it is one SO LINE's remainder, while the operation it
    pins belongs to the BATCH Rule 1 clubbed that line into. Taking it dropped
    281 pieces of a clubbed order into no plan at all (live 2026-08-11). The
    quantity comes from ``Job.qty_for`` — the same expression every other
    placement in this module uses.

    Rows are collapsed to ONE pin per (job, op): several clubbed lines can each
    be in progress on the same step, and an operation runs once, on one machine.

    A pin the current routing or the current crew cannot honour is dropped and
    that step falls through to normal scheduling — the alternative is a machine
    list of exactly one impossible machine, which takes the whole book out of the
    plan at the horizon. WHO runs it is not re-pinned here at all: the operator
    is decided by the genome's roster, which qualifies people from Settings alone
    (live 2026-08-03 — removing a machine from someone with work in progress
    froze them straight back onto it).
    """
    pins: dict = {}
    dropped: list = []
    for row in frozen or ():
        key = row_value(row, "order_key", "job_key", "batch_id")
        seq = row_value(row, "op_seq", "process_seq")
        mid = row_value(row, "machine_id", "machine")
        if key is None or seq is None or mid is None:
            dropped.append((row, "the row names no job, step or machine"))
            continue
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            dropped.append((row, f"step {seq!r} is not a number"))
            continue
        key, mid = str(key), str(mid)
        js = state.get(key)
        if js is None:
            dropped.append((row, f"no job {key!r} in this plan"))
            continue
        if mid not in machines:
            dropped.append((row, f"{mid} is not in the Machine master"))
            continue
        op = next((o for o in js.job.ops if o.seq == seq), None)
        if op is None:
            dropped.append((row, f"{key} has no step {seq} in its routing"))
            continue
        if mid not in op.machine_options:
            dropped.append((row, f"{key} step {seq} no longer runs on {mid}"))
            continue
        if not _staffable(mid, shop):
            dropped.append(
                (row, f"no operator in Settings is qualified for {mid}"))
            continue
        rank = pin_rank(row, mid)
        current = pins.get((key, seq))
        if current is None:
            pins[(key, seq)] = (rank, mid, row)
        elif rank < current[0]:
            dropped.append((current[2], _supersede_reason(mid, current[1])))
            pins[(key, seq)] = (rank, mid, row)
        else:
            dropped.append((row, _supersede_reason(current[1], mid)))

    # ``state`` is built in the genome's job order, so a job's position in it IS
    # its priority — the tie-break when two parts were started at the same moment
    # in the previous plan.
    position = {key: pos for pos, key in enumerate(state)}
    for (key, seq), (rank, mid, _row) in sorted(pins.items()):
        js = state[key]
        js.pinned[seq] = mid
        js.pin_rank[seq] = rank
        machines[mid].pinned_jobs.append((rank, position[key], key))
    for ms in machines.values():
        ms.pinned_jobs.sort()
    return dropped
