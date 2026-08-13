"""The shift clock — roster the crew, then flow jobs into the capacity it creates.

This replaces the Giffler-Thompson event loop. That loop repeatedly picks one
operation and places it, grabbing operators opportunistically as a side effect, so
there is no moment in it at which the question "is there a machine with work and a
free qualified operator right now?" even exists. Here that question is asked once
per shift, and answered exactly.

    for each shift:
        1. roster  — who mans what (roster.py, an exact assignment)
        2. run     — advance every manned machine through the shift
        3. advance

Two shop rules are STRUCTURAL here, not checked afterwards:

  * one operator mans one CNC/VMC for a whole shift — the roster is computed once
    per window and an operator appears in exactly one machining machine's roster,
    so a hopping schedule cannot be expressed there. Manual and inspection
    stations are staffed by INTERVAL EXCLUSION instead: a helper physically walks
    between deburring and packing, so they may serve several benches in a shift,
    but never two at the same instant (``_bench_operator``). Locking a helper to
    one bench for the whole shift would also remove the double-booking, and it
    deletes capacity the shop really has — measured at +52% late-days on books
    where helpers are scarcer than benches;
  * an operation runs to completion on its machine — a machine holds ONE job from
    the moment it claims it until the moment it finishes, across shift boundaries
    and dark shifts alike, so there is no hole another job could be dropped into.
    A helper who starts a bench is booked for the whole projected run, so the
    person cannot walk away mid-operation either.

Within a shift the manned machines are advanced by an event loop rather than one
at a time to the end of the window: at each step the machine that can act EARLIEST
moves. That is what lets a successor released by overlap at 10:50 actually start at
10:50 on a machine that has been idle since 08:00, instead of losing the shift
because its feeder happened to be processed later in a fixed machine order.

Determinism is a hard requirement — the optimizer builds thousands of plans and
must get byte-identical output for identical input. Every collection that feeds a
decision is sorted (machines by id, jobs by the caller's sequence); no set or dict
iteration order reaches the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from roster_engine import release as rel
from roster_engine import roster as crew
from roster_engine.domain import DISPATCH, MACHINING, OUTSOURCED
from roster_engine.worktime import iter_shifts, machine_runs_shift, operator_shift

_EPS = 1e-9
_SECOND = timedelta(seconds=1)

# How many consecutive shift windows may pass with nothing placed while work is
# still outstanding before we stop walking the horizon. A single outsourcing block
# is the longest legitimate quiet spell (the real book carries up to 264 h ≈ 22
# working shifts); 120 windows is ~60 days, comfortably clear of it and still fast
# enough that an unstaffable machine is answered in milliseconds instead of after
# the whole 400-day horizon.
#
# It is a SPEED bound, not a verdict. Whatever it stops short of is handled by the
# same degrade path as a genuinely impossible step, and ``_blocked_reason`` checks
# Settings before naming a cause — so a merely slow book is no longer reported as
# a missing qualification.
_STALL_WINDOWS = 120

# Several clubbed SO lines can each be part-finished on the SAME batch operation,
# so freeze legitimately emits more than one row for it. An operation runs once,
# on one machine, so exactly one of those rows becomes the pin and the others are
# reported under this reason — not an error, but it keeps the row accounting in
# `Plan.unpinned` exact rather than approximately exact.
#
# It is the reason ONLY when the rows agree on the machine. Two rows naming
# DIFFERENT machines for one operation is a data CONFLICT, not a duplicate, and
# calling it "superseded" attributes a cause nobody checked (the 2026-08-09 rule).
# The reviewer saw this message 8-11 times per book on a WIP run, i.e. on the
# owner's screen, so the two are told apart by ``_supersede_reason``.
_SUPERSEDED = "superseded by an earlier-started row for the same operation"
_CONFLICT = ("two in-progress rows name DIFFERENT machines for one operation "
             "(%s and %s); it runs once, so %s was kept — the older start wins")


def _supersede_reason(kept_mid: str, dropped_mid: str) -> str:
    """Why a second row for one (job, op) was not applied. CHECKED, not assumed:
    same machine -> a duplicate; different machine -> a real conflict."""
    if str(kept_mid) == str(dropped_mid):
        return _SUPERSEDED
    return _CONFLICT % (kept_mid, dropped_mid, kept_mid)


class Unschedulable(RuntimeError):
    """NOTHING in this book could be planned — not one job reached its last step.

    Deliberately narrow. One machine losing its only qualified operator must cost
    that machine's ORDERS their plan, never the whole book's (CLAUDE.md principle
    5(a): a master-data gap is non-blocking and skips only the affected order, and
    a routing naming a machine the Machine master has not caught up with is a
    documented, ongoing condition). ``schedule`` therefore DROPS the jobs it could
    not place and reports them in ``Plan.dropped``; this is raised only when that
    would leave nothing at all, which really is a book the shop cannot run.

    A ``RuntimeError`` subclass, so a caller that only knows the base class is
    unaffected. It additionally carries ``blocked`` — ``((job_key, op_seq,
    op_name, machine_options, reason), ...)`` — because this package deliberately
    knows nothing about the application embedding it and therefore cannot raise
    that application's own localized error type. The embedder reads ``blocked``
    and re-raises in its own terms; without it, the only way to name the offending
    job is to parse the message back apart.
    """

    def __init__(self, message: str, blocked=()):
        super().__init__(message)
        self.blocked = tuple(blocked)


class PlanStartMissing(ValueError):
    """``config.plan_start_date`` was None when the engine was called.

    A caller bug, not a shop condition — the API boundary resolves the nullable
    "auto: start from today" to a real date and the pure engine must never see
    None (CLAUDE.md, ``engine/config.py``). It is TYPED so the seam can contain it
    as a localized ``RuleError`` instead of letting a bare ``ValueError`` unwind
    ``run_forward`` and blank every per-rule tab: ``roster_adapter.run`` catches
    only what it names, and it used to name ``Unschedulable`` alone.
    """


@dataclass(frozen=True)
class Placement:
    """One operation, placed. ``segments`` is the per-shift operator record:
    ((start, end, operator), ...), one entry per shift the operation crossed.

    ``work_min`` is the machine time the operation actually consumed. It can be
    less than ``end - start`` for two honest reasons: the operation was HELD over
    a dark shift or a weekly off, and/or its end was paced out to its
    predecessor's (see ``_MachineState.pace_floor``)."""

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
    """``unpinned`` is the frozen rows this plan could NOT honour, as
    ``((row, reason), ...)`` — see ``_apply_frozen``. It defaults to empty, so a
    caller that does not care is unaffected; a caller that does can check the
    accounting closes (every row in is either an applied pin or a row here).

    ``dropped`` is the jobs no shift in the horizon could place, as
    ``((job_key, op_seq, op_name, machine_options, reason), ...)``. They are in
    NO plan — every placement they had made is removed, so an order is either
    fully planned or absent, never half-drawn on the Gantt. A dropped job must
    never be silent: the seam turns each of these into a note (see
    ``roster_adapter.run``), which is how ``build_jobs`` already reports the
    NO_ROUTING skips this is the sibling of."""

    placements: tuple
    completion: dict
    unpinned: tuple = ()
    dropped: tuple = ()


class _JobState:
    __slots__ = ("job", "idx", "ready", "prev_end", "worked", "on_machine",
                 "released_at", "pinned", "pin_rank", "pin_operator")

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
        self.pin_operator = {}         # op_seq -> who the LAST plan had on it


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


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def schedule(jobs, sequence, shop, config, *, overlap=1.0, crew_rank=None,
             frozen=None) -> Plan:
    """Build a plan. ``sequence`` is the job order — the optimizer's first lever;
    ``crew_rank`` is machine id -> rank — its second."""
    crew_rank = dict(crew_rank or {})
    setup_min = float(getattr(config, "setup_time_min", 90.0) or 0.0)
    plan_start = _plan_start(config)

    by_key = {j.key: j for j in jobs}
    order = _order(by_key, sequence)
    state = {key: _JobState(by_key[key], plan_start) for key in order}
    machines = {mid: _MachineState() for mid in sorted(shop.machines)}

    placements: list = []
    completion: dict = {}
    unpinned = _apply_frozen(frozen, state, machines, shop)

    stalled = 0
    for window in iter_shifts(plan_start, shop.calendar, config):
        if not _outstanding(state):
            break
        cursor = max(window.start, plan_start)
        if cursor >= window.end:
            continue
        before = len(placements)
        placements.extend(_settle_milestones(state, window.end, completion))
        _run_shift(window, cursor, order, state, machines, shop, crew_rank,
                   overlap, setup_min, placements, completion)
        stalled = 0 if len(placements) > before else stalled + 1
        if stalled > _STALL_WINDOWS:
            break

    # DEGRADE AT THE PLACEMENT-FAILURE BOUNDARY. One unstaffable machine used to
    # cost the ENTIRE book its plan — including every order that never touches it
    # — which inverts CLAUDE.md principle 5(a) (a master-data gap is non-blocking
    # and skips only the affected order) and would blank the owner's whole
    # schedule the day a routing names a machine the Machine master has not caught
    # up with. The jobs that could not be placed are dropped and REPORTED; the
    # rest of the book is planned.
    unfinished = [key for key in order if state[key].idx < len(state[key].job.ops)]
    blocked = tuple(_blocked_row(state[key], shop) for key in unfinished)
    if unfinished and len(unfinished) == len(order):
        # Nothing at all could be planned. That is not one order's data gap, it is
        # a book the shop cannot run — fail loud (RULES.md), never hand back an
        # empty schedule that reads as "no work to do".
        raise Unschedulable(
            "roster scheduler could not place a single order within the horizon: "
            + _blocked_text(blocked), blocked)
    if unfinished:
        # An order is either fully planned or absent — a half-laid routing on the
        # Gantt would be a worse lie than an omission.
        gone = set(unfinished)
        placements = [p for p in placements if p.job_key not in gone]
        for key in gone:
            completion.pop(key, None)

    placements = _pace(placements, completion)
    return Plan(tuple(placements), completion, tuple(unpinned), blocked)


def _blocked_row(js, shop):
    op = js.job.ops[js.idx]
    options = tuple(op.machine_options)
    return (js.job.key, op.seq, op.name, options, _blocked_reason(options, shop))


def _blocked_reason(options, shop) -> str:
    """WHY this step could not be placed — CHECKED against Settings, never
    assumed. The old text asserted "no shift ever had a qualified, rostered
    operator" for every failure, including the ones ``_STALL_WINDOWS`` produces
    out of a merely SLOW book, which is exactly the 2026-08-09 defect: a report
    may not attribute a cause it did not check. When the machines really are
    staffable the honest answer is that the cause is unexplained, and it says so.
    """
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


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

def _plan_start(config) -> datetime:
    """When the clock starts: 08:00 of the plan-start date, or the auto-mode floor
    if it is later.

    ``Config.plan_start_floor`` is an ISO **string**, not a datetime (the brief's
    sketch returned it verbatim, which would have put a str into every comparison
    in this module), and Config has no ``first_shift_start`` time field — it stores
    plain hour ints. ``plan_start_date`` is nullable in Config but the pure engine
    must never see None (api resolves it at the boundary), so a None here is a
    caller bug and says so — through a TYPED error the seam can contain, because a
    bare ``ValueError`` escaping ``run_forward`` discards the whole trace."""
    day = getattr(config, "plan_start_date", None)
    if day is None:
        raise PlanStartMissing(
            "plan_start_date is None — resolve it to a real date at the API "
            "boundary before calling the scheduler")
    base = datetime.combine(day, time(int(config.first_shift_start_hour), 0))
    floor = getattr(config, "plan_start_floor", None)
    if not floor:
        return base
    if isinstance(floor, str):
        floor = datetime.fromisoformat(floor)
    return max(base, floor)


def _order(by_key, sequence) -> list:
    """The job order: the caller's sequence first (de-duplicated, unknown keys
    dropped), then anything the sequence forgot, in job order. Never a set — the
    priority order IS the optimizer's first lever and must be exactly reproducible."""
    seen, order = set(), []
    for key in sequence or ():
        if key in by_key and key not in seen:
            seen.add(key)
            order.append(key)
    for key in by_key:
        if key not in seen:
            seen.add(key)
            order.append(key)
    return order


def _outstanding(state) -> bool:
    return any(js.idx < len(js.job.ops) for js in state.values())


def _current_op(js):
    return js.job.ops[js.idx] if js.idx < len(js.job.ops) else None


def _work_min(job, op, setup_min: float) -> float:
    """Minutes this step occupies its resource: cutting + a setup if it is a
    CNC/VMC step, the flat lead time for an outsourced block, nothing for a
    milestone or a step with nothing left to make."""
    qty = job.qty_for(op.seq)
    if op.kind == DISPATCH or qty <= 0:
        return 0.0
    if op.kind == OUTSOURCED:
        return float(op.cycle_min or 0.0)
    if not op.machine_options:
        return 0.0
    setup = setup_min if op.kind == MACHINING else 0.0
    return setup + qty * float(op.cycle_min or 0.0)


def _setup_min_for(js, op, setup_min: float) -> float:
    """The nominal setup this step would pay, before the machine's own
    already-set-up check. Zero for a FROZEN step: the part is in the chuck and the
    fixture is on — a resumed operation is not set up a second time."""
    if op.seq in js.pinned:
        return 0.0
    return setup_min if op.kind == MACHINING else 0.0


def _setup_for(js, op, ms, setup_min: float) -> float:
    """The setup this operation actually pays on this machine: none when it is
    resumed (frozen), none when the machine is already set up for the same
    (item, process), the nominal figure otherwise."""
    if op.seq in js.pinned:
        return 0.0
    if op.kind != MACHINING or ms.last_key == (js.job.item_code, op.seq):
        return 0.0
    return setup_min


# --------------------------------------------------------------------------- #
# Milestones — no machine, no crew
# --------------------------------------------------------------------------- #

def _settle_milestones(state, now, completion) -> list:
    """Place every OS / DISPATCH / nothing-left-to-make step that is ready by
    ``now``.

    DISPATCH is placed at the latest end across the WHOLE batch, never at its
    immediate predecessor's release point — an order is dispatched only once every
    piece has cleared every process (RULES.md:305). ``js.prev_end`` is exactly that
    running maximum.
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
                # Fully sequential both sides: nothing overlaps INTO a vendor block
                # (release.overlaps says so) and js.ready is its predecessor's end.
                start = js.ready
                if start > now:
                    continue
                lead = float(op.cycle_min or 0.0)
                end = start + timedelta(minutes=lead)
                out.append(Placement(js.job.key, op.seq, op.name, OUTSOURCED, None,
                                     int(max(qty, 0)), start, end, lead, ()))
                _advance(js, end, completion)
            elif op.kind == DISPATCH:
                at = js.prev_end
                if at > now:
                    continue
                # The batch quantity, the same as every other placement here and
                # the same as the engine this replaces writes on an off-machine
                # milestone. The dispatch row is displayed and exported like any
                # other, so a milestone reading "Qty 0" is a visible difference
                # that has nothing to do with scheduling.
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
        # nothing pipelines (OS, a milestone, a zero-cycle step, overlap 100%).
        # The brief's `js.ready = max(js.ready, js.prev_end)` here silently undid
        # every release _release_successor had just computed.
        js.ready = js.released_at if js.released_at is not None else js.prev_end
    js.released_at = None


def _release_successor(js, op, segments, overlap, setup_charged):
    """Open the next operation once whole pieces have cleared this one.

    ``setup_charged`` is the setup this machine ACTUALLY put in the segments, not
    the nominal ``config.setup_time_min``: a repeat of the same (item, process) is
    already set up and pays none. Passing the nominal figure here made ``need``
    exceed every minute the machine worked, so ``_release_moment`` clamped to the
    last segment's end and ``released_at == end`` — overlap silently switched
    itself OFF, at every setting, on exactly the repeat-item case the setup-skip
    exists for (2026-08-12 review, finding 2).
    """
    js.released_at = None
    if js.idx + 1 >= len(js.job.ops):
        return
    nxt = js.job.ops[js.idx + 1]
    if not rel.overlaps(op, nxt):
        return
    need = rel.work_min_before_release(js.job, op, overlap, setup_charged)
    js.released_at = _release_moment(segments, need)


def _release_moment(segments, need: float):
    """The wall-clock instant at which the machine had done ``need`` minutes of
    WORK on this operation.

    Worked minutes, not elapsed: an operation held overnight, or over the weekly
    off, cuts nothing in the gap. Measuring from the operation's start would
    release pieces in the middle of a dark shop (the brief's
    ``js.started + need``)."""
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

def _run_shift(window, cursor, order, state, machines, shop, crew_rank, overlap,
               setup_min, placements, completion):
    demand, ready, in_progress = _shift_demand(order, state, machines, window,
                                               overlap, setup_min)
    prefer = _preferred_operators(state, machines)
    rostered = crew.roster_for_shift(window, shop, demand, in_progress, crew_rank,
                                     prefer=prefer, ready=ready)
    floating = _floating_operators(shop, window, set(rostered.values()))

    # A CNC/VMC has ONE person for the whole shift; a bench has a POOL, and who
    # of them is on it is decided per operation, at the moment it starts.
    benches: dict = {}
    booked = {o.name: [] for o in floating}
    manned = {}
    for mid in sorted(shop.machines):
        if not machine_runs_shift(shop.machines[mid], window.shift):
            continue
        if mid in shop.machining_ids:
            who = rostered.get(mid)
            if who is not None:
                manned[mid] = who
            continue
        pool = tuple(o.name for o in floating
                     if mid in (getattr(o, "machines", None) or ()))
        if pool:
            benches[mid] = pool
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
            if machines[mid].job_key is not None:
                start = now                      # the part is already in the chuck
            elif _next_job(mid, machines[mid], order, state, now) is not None:
                start = now
            else:
                # Nothing startable yet — but work released later in THIS shift is
                # still this shift's work; wait for it rather than going home.
                start = _earliest_ready(mid, order, state, now, window.end)
                if start is None:
                    continue
            if best is None or (start, mid) < best:
                best = (start, mid)
        if best is None:
            return
        start, mid = best
        if mid in benches:
            who, retry_at = _bench_operator(mid, machines[mid], order, state,
                                            window, start, setup_min,
                                            benches[mid], booked,
                                            prefer.get(mid))
            if who is None:
                # Every qualified helper is on another bench across this run.
                # The bench waits for one of them — it does not take half a
                # person, and it does not go home for the shift.
                free_at[mid] = min(retry_at, window.end)
                continue
        else:
            who = manned[mid]
        free_at[mid] = _work(mid, machines[mid], order, state, window, start,
                             who, overlap, setup_min, placements, completion)


def _shift_demand(order, state, machines, window, overlap, setup_min):
    """(machine -> minutes it could run this shift, machine -> minutes STANDING at
    it now, machine -> job in it).

    The first map is what the roster maximises coverage of, so it must see work
    that ARRIVES during the window, not only work that is ready at its start: a
    successor released by overlap at 10:50 needs its machine manned from 08:00.
    Each job is therefore walked FORWARD along its routing, carrying the moment
    each step could open, until that moment leaves the window.

    The second map is the SAME walk restricted to each job's CURRENT step — work
    whose predecessors are already done, so it is physically waiting at that
    machine rather than merely projected to arrive. It is not a second
    derivation: it is this walk, tapped one step earlier, so the two can never
    disagree.

    The roster needs BOTH, and reads them for different purposes
    (``roster._match_reserving_benches``). It reserves bench capacity against the
    projection, because benches are staffed reactively during a shift while the
    roster is fixed at its start. But what it is willing to GIVE UP for that
    reservation is judged on this map alone — a machining seat may never be
    surrendered on a promise. Reserving a helper for a bench whose feeder has not
    run is how the deadlock is created in the OTHER direction: one operator, one
    CNC, one bench — the projected bench work takes the operator, the CNC goes
    dark, and the bench work he was reserved for never arrives.

    Every machine holding a part reports at least the minutes still left on it,
    in BOTH maps — a part in the chuck is as standing-there as work gets. The
    roster hands an ``in_progress`` machine a dominating CARRY_BONUS, so the two
    must agree — reporting zero demand for a machine that is nevertheless
    rostered on carry-over would spend a scarce operator on an empty chuck while a
    machine with real work went dark (Task 4 review, carried forward).
    """
    demand: dict = {}
    ready: dict = {}
    for key in order:
        js = state[key]
        ops = js.job.ops
        idx, at, worked = js.idx, js.ready, js.worked
        while idx < len(ops) and at < window.end:
            op = ops[idx]
            # A frozen step pays no setup, so counting one would over-state its
            # demand and its release moment alike.
            eff_setup = _setup_min_for(js, op, setup_min)
            span = _work_min(js.job, op, eff_setup)
            left = max(0.0, span - worked)
            if op.machine_options and left > _EPS:
                # A claimed step can only run where it already is — and so can a
                # frozen one, whose part is physically on its pinned machine.
                # Reporting its minutes against every alternative would man a
                # machine that can never touch it.
                if idx == js.idx and js.on_machine:
                    targets = (js.on_machine,)
                elif op.seq in js.pinned:
                    targets = (js.pinned[op.seq],)
                else:
                    targets = op.machine_options
                for mid in targets:
                    demand[mid] = demand.get(mid, 0.0) + left
                    if idx == js.idx:
                        ready[mid] = ready.get(mid, 0.0) + left
            if idx + 1 >= len(ops):
                break
            if rel.overlaps(op, ops[idx + 1]):
                need = rel.work_min_before_release(js.job, op, overlap, eff_setup)
            else:
                need = span
            at = max(at, window.start) + timedelta(minutes=max(0.0, need - worked))
            worked = 0.0
            idx += 1

    in_progress = {}
    for mid in sorted(machines):
        ms = machines[mid]
        if ms.job_key is not None and ms.remaining > _EPS:
            in_progress[mid] = ms.job_key
            demand[mid] = max(demand.get(mid, 0.0), float(ms.remaining))
            ready[mid] = max(ready.get(mid, 0.0), float(ms.remaining))
    return demand, ready, in_progress


def _floating_operators(shop, window, rostered_names) -> list:
    """Everyone on this shift who is NOT locked to a CNC/VMC — the pool that staffs
    manual and inspection stations, where a person genuinely walks between
    machines (roster.py's docstring: forbidding that would delete capacity that
    really exists)."""
    return [o for o in sorted(shop.operators, key=lambda o: o.name)
            if o.name not in rostered_names
            and operator_shift(o) == window.shift
            and not any(s < window.end and window.start < e
                        for s, e in shop.absent.get(o.name, ()))]


def _bench_run_minutes(mid, ms, order, state, window, start, setup_min):
    """How long this bench would hold a person from ``start``, or None if it has
    nothing to run.

    A pure peek: it mirrors exactly what ``_work`` is about to compute (the same
    claim rule, the same setup-skip, the same clip at the window end) without
    touching a thing. It has to be computed BEFORE the claim, because a bench with
    no free helper must not claim the job at all.
    """
    if ms.job_key is not None:
        remaining = ms.remaining                  # part already on the bench
    else:
        picked = _next_job(mid, ms, order, state, start)
        if picked is None:
            return None
        js, op = picked
        setup = _setup_for(js, op, ms, setup_min)
        remaining = setup + js.job.qty_for(op.seq) * float(op.cycle_min or 0.0)
    return min(max(remaining, 0.0),
               (window.end - start).total_seconds() / 60.0)


def _preferred_operators(state, machines) -> dict:
    """machine id -> the person the LAST plan had on the part that machine is
    holding now. A preference for the roster, never a booking.

    Only the pin that will resume FIRST on each machine is consulted — that is the
    part actually in the chuck; a later pin's person is not owed the shift. Empty
    on an ordinary plan (no machine has any pins), so this costs one pass over the
    machine ids per shift and nothing else.
    """
    prefer: dict = {}
    for mid in sorted(machines):
        for _rank, _pos, key in machines[mid].pinned_jobs:
            js = state[key]
            op = _current_op(js)
            if op is None or js.pinned.get(op.seq) != mid:
                continue          # that part has moved on; this pin is spent
            who = js.pin_operator.get(op.seq)
            if who:
                prefer[mid] = who
            break
    return prefer


def _bench_operator(mid, ms, order, state, window, start, setup_min, pool,
                    booked, prefer=None):
    """Staff a manual / inspection station, by INTERVAL EXCLUSION.

    Returns ``(operator_name, None)`` and books them, or ``(None, retry_at)`` when
    every qualified helper is occupied across the run.

    roster.py rosters only CNC/VMC, on the grounds that a helper physically walks
    between deburring and packing — and the shop rule is exactly the brief's
    prose: any qualified operator who is on that shift, present, and *not already
    occupied at that moment*. The brief's sketch dropped the last clause and
    handed the SAME person to every bench they are qualified for (49 same-instant
    double-bookings on a 50-job book). Locking a helper to one bench for the shift
    fixes that too, but it is a strictly stronger rule than the shop's and costs
    real capacity: 3 benches / 1 helper / an hour each took 3 days with the helper
    idle 600 of 660 minutes a shift, and 50 jobs / 6 stations / 2 helpers went to
    41 late-days against 27 (2026-08-12 review, finding 1).

    The person is booked for the WHOLE projected run, not per minute: an operation
    is never interrupted, so a helper who starts a bench stays on it until the
    operation finishes or the shift ends. Between operations they are free to
    walk. Ties resolve by name (``pool`` is name-sorted), so the caller's operator
    order cannot move a bench — except that ``prefer``, the person the last plan
    had on the part now on this bench, is tried first. That is a tie-break among
    people who are all equally entitled to the bench, and it is what stops a
    re-plan renaming the operator on a job that is physically running (finding 4);
    the clash check below still applies to them exactly as to anyone else, so a
    preferred helper who is busy elsewhere simply loses.
    """
    minutes = _bench_run_minutes(mid, ms, order, state, window, start, setup_min)
    if minutes is None:
        return None, window.end                   # nothing here to staff
    if prefer and prefer in pool:
        pool = (prefer,) + tuple(n for n in pool if n != prefer)
    finish = start + timedelta(minutes=minutes)
    retry_at = None
    for name in pool:
        clash = None
        for busy_start, busy_end in booked[name]:
            if busy_start < finish and start < busy_end:
                clash = busy_end if clash is None else max(clash, busy_end)
        if clash is None:
            booked[name].append((start, finish))
            return name, None
        if retry_at is None or clash < retry_at:
            retry_at = clash
    return None, (retry_at if retry_at is not None else window.end)


def _next_job(mid, ms, order, state, now):
    """The next job this machine should start: a part it is already physically
    holding first, then the caller's sequence order.

    Only jobs that are ready NOW and that no other machine has claimed. The claim
    is what stops two alternative machines from both building the same batch —
    ``_next_job`` sees only job state, so without it a step listing "CNC3/CNC7"
    is run twice, once on each.

    A FROZEN step may run only on the machine it is pinned to, and the frozen
    steps this machine is holding go first, in previous-plan start order: the shop
    cannot start a new part in a chuck that already holds a half-finished one
    (``ms.pinned_jobs``, built once by ``_apply_frozen`` and already in that
    order). That is a preference among things that are all READY — the routing
    gate is the same one every other step passes, so a frozen step still never
    runs before the step that feeds it (2026-08-09).

    ``ms.pinned_jobs`` is empty on an ordinary plan, so the frozen pass costs
    nothing and the sequence scan still returns on its first hit; walking the
    whole order to look for pins instead cost ~18% of a plan (68-job book).
    """
    for _rank, _pos, key in ms.pinned_jobs:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or js.pinned.get(op.seq) != mid:
            continue
        if js.ready > now or js.job.qty_for(op.seq) <= 0:
            continue
        return js, op
    for key in order:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or mid not in op.machine_options:
            continue
        if js.pinned and js.pinned.get(op.seq, mid) != mid:
            continue                      # frozen elsewhere; not this machine's
        if js.ready > now or js.job.qty_for(op.seq) <= 0:
            continue
        return js, op
    return None


def _earliest_ready(mid, order, state, after, before):
    """The first moment strictly after ``after`` and before ``before`` at which
    some unclaimed job could start on this machine."""
    best = None
    for key in order:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or mid not in op.machine_options:
            continue
        if js.pinned and js.pinned.get(op.seq, mid) != mid:
            continue                      # this machine can never take that part
        if js.job.qty_for(op.seq) <= 0:
            continue
        if js.ready <= after or js.ready >= before:
            continue
        if best is None or js.ready < best:
            best = js.ready
    return best


def _work(mid, ms, order, state, window, start, operator, overlap, setup_min,
          placements, completion):
    """Advance one machine from ``start``. Returns when it is next free.

    The machine claims a job and does not let go until the operation is finished,
    so no other job can be squeezed into it — that is the no-segmentation
    guarantee, and it is why this returns a free time rather than a placement per
    shift.
    """
    if ms.job_key is None:
        picked = _next_job(mid, ms, order, state, start)
        if picked is None:
            return window.end                     # nothing to do; park it
        js, op = picked
        # Setup is charged once per OPERATION (not per shift), not at all when the
        # same (item, process) is already set up on this machine, and not at all
        # when the operation is RESUMED — a frozen part is already in the chuck.
        setup = _setup_for(js, op, ms, setup_min)
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
        # A step with a machine but no work at all (zero cycle time, no setup):
        # a zero-length segment, so the chain still advances and it is visible.
        #
        # The segment is recorded even though it consumes nothing, because this
        # placement is NOT invisible downstream: overlap can open it early and
        # ``pace_floor`` holds its end to its predecessor's, so the published
        # entry spans real wall-clock time. A reader that trusts the operator
        # record — the Gantt bar, the delay report's running-op list, and above
        # all ``freeze``, which pins machine AND operator — would otherwise get a
        # machine placement with nobody on it and freeze a ghost. The person IS
        # standing at the bench: ``operator`` is right here.
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
    _release_successor(js, op, ms.segments, overlap, ms.setup_charged)
    ms.job_key = ms.op_seq = ms.pace_floor = None
    ms.segments, ms.started, ms.setup_charged = [], None, 0.0
    _advance(js, end, completion)
    placements.extend(_settle_milestones(state, window.end, completion))
    return end


# --------------------------------------------------------------------------- #
# Finishing
# --------------------------------------------------------------------------- #

def _pace(placements, completion) -> list:
    """A step's END is never before an earlier step's END (RULES.md:132), and the
    job's completion is the latest end it has.

    The real work is done at claim time (``_MachineState.pace_floor``), which
    extends the machine's occupancy with the operation so nothing is dropped into
    the paced tail. This is the belt-and-braces sweep that makes the invariant
    true of the published plan whatever produced it — it should never bind for a
    machine placement, and does bind for nothing in the current engine.
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


# --------------------------------------------------------------------------- #
# Reading a frozen row — THE authoritative definitions
#
# ``engine.roster_adapter`` translates SO-LINE rows into BATCH pins before this
# module ever sees them, and it needs the same three answers (which spelling
# carries the value, how a previous-plan start sorts, which of two rows for one
# operation wins). It IMPORTS these rather than keeping its own copies: they used
# to be duplicated with different rank tuples, one edit away from the two halves
# disagreeing about which pin survives.
# --------------------------------------------------------------------------- #

def row_value(row, *names):
    """One value out of a frozen row, whatever it is spelled and whatever type it
    is. ``engine.freeze.compute_frozen_set`` emits ``machine`` and an ISO STRING
    ``prev_start``; the app's adapter renames some of that to ``machine_id`` /
    ``order_key``. A pin that silently does nothing because of a key name is the
    worst failure available here — the plan looks fine and the part is on the
    wrong machine — so both spellings are read, and a row is only ever ignored on
    its merits. Blank counts as absent, so an empty ``machine`` falls through to
    the next spelling rather than pinning to ""."""
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
    previous-plan start, then machine, operator and SO number so the answer can
    never depend on the order the rows arrive in. ``mid`` is passed in because the
    adapter has already normalised it and this module has not."""
    return (prev_start_key(row), str(mid),
            str(row_value(row, "operator") or ""),
            str(row_value(row, "so_no") or ""))


def _staffable(mid, shop) -> bool:
    """Could ANYBODY in Settings man this machine, on a shift it runs?

    Qualification is exactly the machine list the admin set (roster.eligible says
    the same, for the same 2026-08-07 reason). Absences are deliberately NOT
    consulted: they are dated windows, not a statement that the machine has lost
    its crew, and a pin dropped because somebody took a Tuesday off would be a
    worse error than the one this guards.
    """
    machine = shop.machines.get(mid)
    for operator in shop.operators:
        if mid not in (getattr(operator, "machines", None) or ()):
            continue
        if machine is None:
            return True
        if machine_runs_shift(machine, operator_shift(operator)):
            return True
    return False


def _apply_frozen(frozen, state, machines, shop):
    """Pin every in-progress operation to the machine it is physically on, and
    return ``[(row, reason)]`` for every row whose pin could NOT be applied.

    THE ACCOUNTING CLOSES: each row that arrives is either the applied pin for its
    (job, op) or it is in the returned list, so ``len(frozen) == pins + len(
    dropped)``. A dropped pin used to be silent, and against the real producer
    (``engine.freeze.compute_frozen_set``, whose rows this engine had never
    actually been fed) the drop rate was 100% — the plan looked well-formed while
    the part was planned on a machine it is not on, paying a setup it does not
    owe. A silently dropped constraint is a named recurring defect class here, so
    the caller is told rather than trusted to notice.

    A frozen row pins WHERE and WHEN, never HOW MUCH. ``remaining_qty`` is
    deliberately NOT read: it is one SO LINE's remainder, while the operation it
    pins belongs to the BATCH Rule 1 clubbed that line into. Taking it dropped 281
    pieces of a clubbed order into no plan at all (live 2026-08-11). The quantity
    comes from ``Job.qty_for`` — the same expression every other placement in this
    module uses — because the pin never touches quantity at all.

    Rows are collapsed to ONE pin per (job, op): several clubbed lines can each be
    in progress on the same step, and an operation runs once, on one machine. The
    surviving row is the earliest-started one, with the machine and operator names
    breaking a tie, so the answer cannot depend on the order rows arrive in.

    A pin the CURRENT routing cannot honour — an unknown job or step, a machine
    that has left the master, or a step whose routing no longer lists that machine
    — is dropped, and that step falls through to normal scheduling. This mirrors
    ``engine.freeze``, which does not freeze a step it cannot resolve; the
    alternative is a machine list of exactly one impossible machine, which takes
    the whole book out of the plan at the horizon.

    A pin the CREW cannot honour is dropped for the same reason and it is the same
    hazard: if Settings no longer qualifies anybody for the pinned machine, that
    one job can never be placed and ``schedule`` fails the ENTIRE book at the
    horizon. One machine losing its operator must not cost every other order its
    plan — that condition is real, but the app already reports it on its own
    (MACHINE_NO_OPERATOR), and degrading here is what the engine this replaces
    does ("unstaffable — leave to the main loop").

    ``operator`` breaks ties, and is remembered as a PREFERENCE (``pin_operator``)
    — never as a re-pin. Who runs the machine is decided by the roster, which
    qualifies people from Settings alone (live 2026-08-03 — removing a machine
    from someone with work in progress froze them straight back onto it). But
    ignoring the name entirely churns the shift-wise sheet, a live floor document,
    by re-assigning the person on a physically-running job for no reason at all;
    ``roster.roster_for_shift``'s ``prefer`` keeps them there when they are still
    qualified and free, and yields to anything real.
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

    # ``state`` is built in the caller's sequence order, so a job's position in it
    # IS its priority — the tie-break when two parts were started at the same
    # moment in the previous plan.
    position = {key: pos for pos, key in enumerate(state)}
    for (key, seq), (rank, mid, row) in sorted(pins.items()):
        js = state[key]
        js.pinned[seq] = mid
        js.pin_rank[seq] = rank
        who = str(row_value(row, "operator") or "")
        if who:
            js.pin_operator[seq] = who
        machines[mid].pinned_jobs.append((rank, position[key], key))
    for ms in machines.values():
        ms.pinned_jobs.sort()
    return dropped
