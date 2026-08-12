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

  * one operator mans one machine for a whole shift — the roster is computed once
    per window and an operator appears in exactly one machine's roster, so a
    hopping schedule cannot be expressed;
  * an operation runs to completion on its machine — a machine holds ONE job from
    the moment it claims it until the moment it finishes, across shift boundaries
    and dark shifts alike, so there is no hole another job could be dropped into.

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
from roster_engine.assign import max_weight_matching
from roster_engine.domain import DISPATCH, MACHINING, OUTSOURCED
from roster_engine.worktime import iter_shifts, machine_runs_shift, operator_shift

_EPS = 1e-9
_SECOND = timedelta(seconds=1)

# How many consecutive shift windows may pass with nothing placed while work is
# still outstanding before we call it impossible rather than merely slow. A single
# outsourcing block is the longest legitimate quiet spell (the real book carries up
# to 264 h ≈ 22 working shifts); 120 windows is ~60 days, comfortably clear of it
# and still fast enough that an unstaffable machine fails in milliseconds instead
# of walking the whole 400-day horizon.
_STALL_WINDOWS = 120


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
    placements: tuple
    completion: dict


class _JobState:
    __slots__ = ("job", "idx", "ready", "prev_end", "worked", "on_machine",
                 "released_at")

    def __init__(self, job, plan_start):
        self.job = job
        self.idx = 0
        self.ready = plan_start        # when the CURRENT op may start
        self.prev_end = plan_start     # latest end across every op done so far
        self.worked = 0.0              # worked minutes on the op in progress
        self.on_machine = None         # machine that has CLAIMED the current op
        self.released_at = None        # overlap release moment for the NEXT op


class _MachineState:
    __slots__ = ("job_key", "op_seq", "remaining", "last_key", "segments",
                 "started", "pace_floor", "busy_until")

    def __init__(self):
        self.job_key = None
        self.op_seq = None
        self.remaining = 0.0
        self.last_key = None           # (item_code, op_seq) of the last job run
        self.segments = []
        self.started = None
        self.pace_floor = None         # this op may not END before its predecessor
        self.busy_until = None         # occupied to here, ACROSS shift boundaries


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
    _apply_frozen(frozen, state, machines, by_key)

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

    unfinished = [key for key in order if state[key].idx < len(state[key].job.ops)]
    if unfinished:
        # Fail loud rather than silently under-schedule (RULES.md). Name the
        # blocking step and the machines it wants: the overwhelmingly likely cause
        # is a machine no operator in Settings is qualified for, or one a routing
        # points at that is not in the Machine master at all.
        blocked = []
        for key in unfinished[:5]:
            js = state[key]
            op = js.job.ops[js.idx]
            blocked.append(f"{key} step {op.seq} '{op.name}' on "
                           f"{'/'.join(op.machine_options) or '(no machine)'}")
        raise RuntimeError(
            "roster scheduler could not place every operation within the horizon "
            "— no shift ever had a qualified, rostered operator for: "
            + "; ".join(blocked))

    placements = _pace(placements, completion)
    return Plan(tuple(placements), completion)


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
    caller bug and says so."""
    day = getattr(config, "plan_start_date", None)
    if day is None:
        raise ValueError(
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
                out.append(Placement(js.job.key, op.seq, op.name, DISPATCH, None,
                                     0, at, at, 0.0, ()))
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


def _release_successor(js, op, segments, overlap, setup_min):
    """Open the next operation once whole pieces have cleared this one."""
    js.released_at = None
    if js.idx + 1 >= len(js.job.ops):
        return
    nxt = js.job.ops[js.idx + 1]
    if not rel.overlaps(op, nxt):
        return
    need = rel.work_min_before_release(js.job, op, overlap, setup_min)
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
    demand, in_progress = _shift_demand(order, state, machines, window, overlap,
                                        setup_min)
    rostered = crew.roster_for_shift(window, shop, demand, in_progress, crew_rank)
    floating = _floating_operators(shop, window, set(rostered.values()))
    stationed = _station_operators(shop, window, demand, in_progress, floating)

    manned = {}
    for mid in sorted(shop.machines):
        if not machine_runs_shift(shop.machines[mid], window.shift):
            continue
        who = (rostered if mid in shop.machining_ids else stationed).get(mid)
        if who is not None:
            manned[mid] = who
    if not manned:
        return                      # every machine dark; anything in a chuck HOLDS

    free_at = {}
    for mid in manned:
        busy = machines[mid].busy_until
        free_at[mid] = max(cursor, busy) if busy else cursor

    while True:
        best = None
        for mid in sorted(manned):
            now = free_at[mid]
            if now >= window.end - _SECOND:
                continue
            if machines[mid].job_key is not None:
                start = now                      # the part is already in the chuck
            elif _next_job(mid, order, state, now) is not None:
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
        free_at[mid] = _work(mid, machines[mid], order, state, window, start,
                             manned[mid], overlap, setup_min, placements,
                             completion)


def _shift_demand(order, state, machines, window, overlap, setup_min):
    """(machine -> minutes of work it could run this shift, machine -> job in it).

    This is what the roster maximises coverage of, so it must see work that
    ARRIVES during the window, not only work that is ready at its start: a
    successor released by overlap at 10:50 needs its machine manned from 08:00.
    Each job is therefore walked FORWARD along its routing, carrying the moment
    each step could open, until that moment leaves the window.

    Every machine holding a part reports at least the minutes still left on it.
    The roster hands an ``in_progress`` machine a dominating CARRY_BONUS, so the
    two must agree — reporting zero demand for a machine that is nevertheless
    rostered on carry-over would spend a scarce operator on an empty chuck while a
    machine with real work went dark (Task 4 review, carried forward).
    """
    demand: dict = {}
    for key in order:
        js = state[key]
        ops = js.job.ops
        idx, at, worked = js.idx, js.ready, js.worked
        while idx < len(ops) and at < window.end:
            op = ops[idx]
            span = _work_min(js.job, op, setup_min)
            left = max(0.0, span - worked)
            if op.machine_options and left > _EPS:
                # A claimed step can only run where it already is.
                targets = ((js.on_machine,) if idx == js.idx and js.on_machine
                           else op.machine_options)
                for mid in targets:
                    demand[mid] = demand.get(mid, 0.0) + left
            if idx + 1 >= len(ops):
                break
            if rel.overlaps(op, ops[idx + 1]):
                need = rel.work_min_before_release(js.job, op, overlap, setup_min)
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
    return demand, in_progress


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


def _station_operators(shop, window, demand, in_progress, floating) -> dict:
    """Man the manual / inspection stations the CNC roster deliberately leaves out.

    roster.py rosters only CNC/VMC, on the grounds that a helper physically walks
    between deburring and packing. The brief took that to mean each station may
    take "any floating qualified person", which hands the SAME person to every
    bench they are qualified for — measured on a 60-job synthetic book that is 11
    windows in which one helper is bolted to two benches at the same instant.
    Walking between benches means being at one of them at a time, so a station
    gets a person of its own for the shift, decided by the same exact assignment
    the CNC roster uses (assign.max_weight_matching) with the same two rules: a
    machine with no work is left dark, and a bench with a part on it keeps its
    person (CARRY_BONUS) whatever else is waiting.
    """
    machines = sorted(mid for mid in shop.machines
                      if mid not in shop.machining_ids
                      and machine_runs_shift(shop.machines[mid], window.shift))
    if not machines or not floating:
        return {}
    values = {}
    for r, operator in enumerate(floating):
        qualified = getattr(operator, "machines", None) or ()
        for c, mid in enumerate(machines):
            if mid not in qualified:
                continue
            pending = float(demand.get(mid, 0.0))
            if pending <= 0.0 and mid not in in_progress:
                continue                      # never man a bench with no work
            value = min(window.minutes, pending)
            if mid in in_progress:
                value += crew.CARRY_BONUS
            values[(r, c)] = value
    matched = max_weight_matching(values, len(floating), len(machines))
    return {machines[c]: floating[r].name for r, c in matched.items()}


def _next_job(mid, order, state, now):
    """The next job this machine should start: the caller's sequence order.

    Only jobs that are ready NOW and that no other machine has claimed. The claim
    is what stops two alternative machines from both building the same batch —
    ``_next_job`` sees only job state, so without it a step listing "CNC3/CNC7"
    is run twice, once on each.
    """
    for key in order:
        js = state[key]
        if js.on_machine is not None:
            continue
        op = _current_op(js)
        if op is None or mid not in op.machine_options:
            continue
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
        picked = _next_job(mid, order, state, start)
        if picked is None:
            return window.end                     # nothing to do; park it
        js, op = picked
        # Setup is charged once per OPERATION (not per shift), and not at all when
        # the same (item, process) is already set up on this machine.
        setup = (setup_min if op.kind == MACHINING
                 and ms.last_key != (js.job.item_code, op.seq) else 0.0)
        ms.job_key, ms.op_seq = js.job.key, op.seq
        ms.remaining = setup + js.job.qty_for(op.seq) * float(op.cycle_min or 0.0)
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
        # a zero-duration placement, so the chain still advances and it is visible.
        seg_end = start
        if ms.started is None:
            ms.started = start

    end = max(seg_end, ms.pace_floor) if ms.pace_floor else seg_end
    work = sum((e - s).total_seconds() / 60.0 for s, e, _w in ms.segments)
    placements.append(Placement(js.job.key, op.seq, op.name, op.kind, mid,
                                int(js.job.qty_for(op.seq)), ms.started, end,
                                work, tuple(ms.segments)))
    ms.last_key = (js.job.item_code, op.seq)
    ms.busy_until = end
    _release_successor(js, op, ms.segments, overlap, setup_min)
    ms.job_key = ms.op_seq = ms.pace_floor = None
    ms.segments, ms.started = [], None
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


def _apply_frozen(frozen, state, machines, by_key):
    """Pin in-progress operations to the machine they are physically on.

    Implemented in Task 6. Until then this is a documented no-op, so ``frozen=None``,
    ``frozen=[]`` and no argument at all are identical.
    """
    return
