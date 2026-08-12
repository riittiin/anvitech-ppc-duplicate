"""Invariants CHECKED, not merely intended.

Two rules of this codebase, both learned the hard way: a class of defect that
ships silently once ships silently again unless an invariant is checked on every
plan (2026-08-07), and a report may never attribute a cause it did not check
(2026-08-09).

All pure, all non-blocking — a live plan must never break because a self-check is
unhappy. Engine-agnostic: they take a ``list[ScheduleEntry]``, so the same four
numbers can be measured on either engine's output and a side-by-side comparison
is evidence rather than assertion.

WHAT THESE CHECKS DELIBERATELY DO NOT FLAG, and why each one matters more than
the thing it does flag: a banner that cries wolf on a legal plan is worse than no
banner, because it teaches the directors to ignore the one row that is real.

  * a 19:00 shift HANDOVER — two machines, one person, two different shifts;
  * a helper WALKING between benches in one shift. ``roster.py`` rosters CNC/VMC
    only, on the shop's own rule that a helper physically walks between deburring
    and packing; locking them to one bench costs real capacity (+52% late-days on
    books where helpers are scarcer than benches). Two benches at the same
    INSTANT is still impossible, and still flagged;
  * an operation HELD in the chuck over a dark shift or the weekly off, with
    nothing else on the machine. That is not segmentation, it is waiting;
  * a machining step that legitimately SKIPS its 90-minute setup because the same
    (item, process) was already on the machine. Assuming a nominal setup turns its
    clean release into a fraction of a piece — a false positive on exactly the
    case the skip exists for;
  * a successor that was released on a whole piece and then waited for its own
    machine, its own operator, or the shop to reopen. The fraction it happens to
    see at its start is contention, not a rounding error, so the row is withheld
    unless nothing can explain the delay;
  * a single-shift bench that is dark at night, or any machine on the weekly off.

The shift clock is read from ``roster_engine.worktime`` — the one definition the
scheduler itself plans by — never re-derived here. A reporting feature that
modelled the shop differently from the engine that built the plan hid 9,470
minutes of real planned work (2026-08-07).
"""

from __future__ import annotations

from datetime import timedelta

from roster_engine.domain import is_machining_machine
from roster_engine.worktime import (FIRST, SECOND, _shift_bounds, iter_shifts,
                                    machine_runs_shift, operator_shift)

# A minute of float/clock slop is not a violation. Expressed in minutes and
# converted to pieces per step, so a long cycle is judged as tightly as a short
# one rather than the tolerance meaning something different on every routing.
_TOL_MIN = 1.0

# The exact strings every off-machine consumer matches on. Duplicated from
# ``engine.roster_adapter`` rather than imported, so this package keeps its zero
# imports from the app's scheduling code and cannot form an import cycle with the
# adapter that calls it; a test pins the two sets equal.
OFF_LANES = {"OS / Outsourced", "Off-machine"}

_KIND_SPLIT = "OPERATOR_SPLIT_SHIFT"
_KIND_SEGMENTED = "OPERATION_SEGMENTED"
_KIND_IDLE = "IDLE_CAPACITY"
_KIND_ROUNDING = "OVERLAP_FRACTIONAL_PIECE"


class _AlwaysOpen:
    """Calendar of last resort, for a masters object that carries none. Never
    used on a real plan — ``engine.models.Masters`` always has one."""

    @staticmethod
    def is_working_day(_day) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Small shared readers
# --------------------------------------------------------------------------- #

def _segments(entry) -> list:
    """``(start, end, operator)`` triples, in start order. The segments are the
    truth about when a machine actually ran: an entry's ``end`` can be paced out
    past its last segment, and an entry held over a dark shift spans far more
    wall-clock time than it occupies."""
    segs = [(s, e, w) for s, e, w in (getattr(entry, "op_segments", None) or ())]
    segs.sort(key=lambda x: (x[0], x[1]))
    return segs


def _covered(entry) -> list:
    """``(start, end)`` intervals the entry really occupies its machine for,
    falling back to its span when it carries no segments at all."""
    segs = _segments(entry)
    return [(s, e) for s, e, _w in segs] or [(entry.start, entry.end)]


def _order(entry):
    return (entry.start, str(entry.batch_id), int(entry.process_seq))


def _on_a_machine(entry) -> bool:
    machine = getattr(entry, "machine", "") or ""
    return bool(machine) and machine not in OFF_LANES


def _strictly_overlap(a0, a1, b0, b1) -> bool:
    """Two intervals share real time. A zero-length interval (a bench step with
    no cycle time) touches nothing — it is an instant, not an occupation."""
    return a0 < b1 and b0 < a1


def _touches(a0, a1, b0, b1) -> bool:
    """As above, but an instant INSIDE the other interval counts."""
    if a0 == a1:
        return b0 <= a0 < b1
    if b0 == b1:
        return a0 <= b0 < a1
    return a0 < b1 and b0 < a1


# --------------------------------------------------------------------------- #
# Which shift a moment belongs to — worktime's clock, not a second copy of it
# --------------------------------------------------------------------------- #

def shift_key(when, config):
    """The ``(day, shift)`` a moment falls in, or ``None`` when the shop is shut.

    ``day`` is the date the shift STARTED on, so an 02:00 segment belongs to the
    PREVIOUS day's night shift — the second shift runs 19:00 -> 05:00. Bounds come
    from ``worktime._shift_bounds``, the same function ``iter_shifts`` builds every
    planning window from.
    """
    today = when.date()
    for day in (today, today - timedelta(days=1)):
        for shift in (FIRST, SECOND):
            start, end = _shift_bounds(day, shift, config)
            if start <= when < end:
                return day, shift
    return None


def _shift_keys(start, end, config) -> list:
    """Every shift an interval touches. Keying on the START alone would put a
    segment that crosses 19:00 in the first shift only; keying on overlap keeps a
    person who really did work both shifts visible in both."""
    keys = []
    day = (start - timedelta(days=1)).date()
    last = end.date()
    while day <= last:
        for shift in (FIRST, SECOND):
            bounds = _shift_bounds(day, shift, config)
            if _touches(start, end, bounds[0], bounds[1]):
                keys.append((day, shift))
        day += timedelta(days=1)
    return keys


def _is_machining(mid: str, masters) -> bool:
    """CNC/VMC, by the Machine master when there is one. The id fallback is the
    same one ``domain._kind_for_machine_id`` uses for a provisional machine, so a
    two-argument call (what Task 11's harness makes) still classifies correctly."""
    machines = (getattr(masters, "machines", None) or {}) if masters is not None else {}
    machine = machines.get(mid)
    if machine is not None:
        return is_machining_machine(machine)
    up = (mid or "").upper()
    return up.startswith("CNC") or up.startswith("VMC")


# --------------------------------------------------------------------------- #
# Rule 1 — one operator, one machine, one shift
# --------------------------------------------------------------------------- #

def operator_split_violations(entries, config, masters=None) -> list:
    """Nobody mans two machines within one shift.

    ``masters`` is optional: it is only used to tell a machining machine from a
    bench, and a CNC/VMC id answers that on its own.
    """
    per: dict = {}
    for entry in entries or ():
        if not _on_a_machine(entry):
            continue
        for start, end, who in _segments(entry):
            if not who:
                continue
            for key in _shift_keys(start, end, config):
                per.setdefault((who, key), {}) \
                   .setdefault(entry.machine, []).append((start, end))

    rows = []
    for (who, key) in sorted(per, key=lambda k: (k[0], k[1][0], k[1][1])):
        machines = per[(who, key)]
        if len(machines) < 2:
            continue
        day, shift = key
        names = sorted(machines)
        clash = _first_clash(machines)
        if clash is not None:
            first, second, at = clash
            message = (f"{who} is planned on {first} and {second} at the same "
                       f"time ({at:%d-%m-%Y %H:%M}) — one person cannot man two "
                       f"machines at once")
        elif any(_is_machining(mid, masters) for mid in names):
            message = (f"{who} is planned on {len(names)} machines in the {shift} "
                       f"shift of {day:%d-%m-%Y} ({', '.join(names)}); one "
                       f"operator mans one machine for a whole shift")
        else:
            # Benches only, never at the same instant: a helper walking between
            # deburring and packing. Legal, and deleting it costs real capacity.
            continue
        rows.append({"kind": _KIND_SPLIT,
                     "ref": f"{who}@{day:%d-%m-%Y}/{shift}",
                     "message": message})
    return rows


def _first_clash(machines: dict):
    """The earliest moment this person is on two machines at once, if any."""
    names = sorted(machines)
    best = None
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            for s0, e0 in machines[first]:
                for s1, e1 in machines[second]:
                    if not _strictly_overlap(s0, e0, s1, e1):
                        continue
                    candidate = (max(s0, s1), first, second)
                    if best is None or candidate < best:
                        best = candidate
    return (best[1], best[2], best[0]) if best else None


# --------------------------------------------------------------------------- #
# Rule 2 — an operation runs to completion
# --------------------------------------------------------------------------- #

def segmentation_violations(entries) -> list:
    """An operation's time on its machine is never interrupted by another job.

    A gap is a violation only when SOMETHING ELSE ran on that machine inside it.
    An overnight or unmanned gap is the part waiting in the chuck, which is legal
    and is what makes this engine's "no holes" guarantee affordable.
    """
    by_machine: dict = {}
    for entry in entries or ():
        if _on_a_machine(entry):
            by_machine.setdefault(entry.machine, []).append(entry)

    rows = []
    for machine in sorted(by_machine):
        items = sorted(by_machine[machine], key=_order)
        for entry in items:
            gaps = _gaps(entry)
            if not gaps:
                continue
            other = _intruder(entry, items, gaps)
            if other is None:
                continue
            rows.append({
                "kind": _KIND_SEGMENTED,
                "ref": f"{entry.batch_id}/{entry.process_seq}",
                "message": (
                    f"'{entry.process_name}' of batch {entry.batch_id} is broken "
                    f"up on {machine}: '{other.process_name}' (batch "
                    f"{other.batch_id}) runs inside its gap. An operation must "
                    f"run to completion.")})
    return rows


def _gaps(entry) -> list:
    segs = _segments(entry)
    return [(a[1], b[0]) for a, b in zip(segs, segs[1:]) if b[0] > a[1]]


def _intruder(entry, items, gaps):
    """The first other job occupying this machine inside one of the gaps.

    Another entry of the SAME operation is not an intruder: the classic engine
    publishes a split step as several entries, and that is a different question
    from another job taking the machine away.
    """
    for other in items:
        if other is entry:
            continue
        if (str(other.batch_id) == str(entry.batch_id)
                and int(other.process_seq) == int(entry.process_seq)):
            continue
        for start, end in _covered(other):
            for gap_start, gap_end in gaps:
                if _strictly_overlap(start, end, gap_start, gap_end):
                    return other
    return None


# --------------------------------------------------------------------------- #
# The owner's original complaint, as a number
# --------------------------------------------------------------------------- #

def idle_capacity_violations(entries, masters, config, absent=None) -> list:
    """A machine with READY work sitting dark while a qualified operator is
    unassigned in that shift.

    Under this engine the roster's whole job is to make this zero; under the live
    engine it is the measurement of what is being lost. Every clause is checked,
    not assumed:

      * the machine runs that shift at all (a bench has no night shift) and the
        day is a working day (``iter_shifts`` reads the shop calendar);
      * the work really was READY — every earlier routing step of that batch had
        finished before the shift began. Work whose feeder was still running could
        not have started however free the crew was;
      * qualification is EXACTLY the Settings machine list. Role is not a gate
        (live 2026-08-07: a CNC was given an operator, dropped from its pool for
        being a workbook "helper", and sat idle with work waiting);
      * the operator is on that shift, is on no machine in it, and is present.

    ``absent`` (optional, ``{operator: [(start, end), ...]}`` — the same shape
    ``Shop.absent`` carries) is the one input the entries cannot supply. Without
    it, a person on leave reads as spare capacity.
    """
    entries = [e for e in (entries or ())]
    if not entries:
        return []
    plan_start = min(e.start for e in entries)
    plan_end = max(e.end for e in entries)

    busy_machine, busy_operator, pending = set(), set(), {}
    for entry in entries:
        real = _on_a_machine(entry)
        for start, end, who in _segments(entry):
            for key in _shift_keys(start, end, config):
                if real:
                    busy_machine.add((entry.machine, key))
                if who:
                    busy_operator.add((who, key))

    ends: dict = {}
    for entry in entries:
        ends.setdefault(str(entry.batch_id), []).append(
            (int(entry.process_seq), entry.end))
    for entry in entries:
        if not _on_a_machine(entry):
            continue
        prior = [end for seq, end in ends[str(entry.batch_id)]
                 if seq < int(entry.process_seq)]
        ready_at = max(prior) if prior else plan_start
        pending.setdefault(entry.machine, []).append((ready_at, entry.start))

    calendar = getattr(masters, "calendar", None) or _AlwaysOpen()
    operators = sorted(getattr(masters, "operators", None) or (),
                       key=lambda o: (o.name or ""))
    rows = []
    for window in iter_shifts(plan_start, calendar, config):
        if window.start >= plan_end:
            break
        key = (window.day, window.shift)
        for mid, machine in sorted((getattr(masters, "machines", None) or {}).items()):
            if (mid, key) in busy_machine:
                continue
            if not machine_runs_shift(machine, window.shift):
                continue
            if not any(ready <= window.start and start >= window.start
                       for ready, start in pending.get(mid, ())):
                continue
            for operator in operators:
                if (operator.name, key) in busy_operator:
                    continue
                if mid not in (getattr(operator, "machines", None) or ()):
                    continue
                if operator_shift(operator) != window.shift:
                    continue
                if _is_absent(absent, operator.name, window):
                    continue
                rows.append({
                    "kind": _KIND_IDLE,
                    "ref": f"{mid}@{window.day:%d-%m-%Y}/{window.shift}",
                    "message": (
                        f"{mid} is dark for the {window.shift} shift of "
                        f"{window.day:%d-%m-%Y} while {operator.name} — qualified "
                        f"for it in Settings and on that shift — is on no machine, "
                        f"and {mid} has work already waiting")})
                break
    return rows


def _is_absent(absent, name, window) -> bool:
    for start, end in (absent or {}).get(name, ()):
        if start < window.end and window.start < end:
            return True
    return False


# --------------------------------------------------------------------------- #
# Whole pieces
# --------------------------------------------------------------------------- #

def overlap_rounding_violations(entries, masters, config=None) -> list:
    """A successor is never released on a fraction of a piece.

    The implied setup is derived from what the entry was ACTUALLY charged
    (``occupancy_min`` less its cutting), never from ``config.setup_time_min``:
    this engine legitimately skips the 90 minutes when the same (item, process)
    is already on the machine, and assuming the nominal figure reads that clean
    release as 0.4 of a piece.

    A fraction alone is not a finding. A successor released on a whole piece and
    then delayed by its own machine, its own operator or the shop being shut sees
    a fraction at its start through no fault of the release, so a row is emitted
    only when nothing explains the delay — and unconditionally when the successor
    started before a single whole piece could exist, which no delay can explain.
    """
    entries = list(entries or ())
    if not entries:
        return []
    if config is None:                      # Task 11 calls the two-argument form
        from engine.config import Config
        config = Config()
    routings = getattr(masters, "routings", None) or {}

    by_batch: dict = {}
    for entry in entries:
        by_batch.setdefault(str(entry.batch_id), {}) \
                .setdefault(int(entry.process_seq), []).append(entry)

    rows = []
    for batch in sorted(by_batch):
        seqs = sorted(by_batch[batch])
        for prev_seq, next_seq in zip(seqs, seqs[1:]):
            prevs, nxts = by_batch[batch][prev_seq], by_batch[batch][next_seq]
            if len(prevs) != 1 or len(nxts) != 1:
                # A step split across machines delivers pieces from several
                # places at once; the arithmetic below cannot speak to it.
                continue
            prev, nxt = prevs[0], nxts[0]
            if not _on_a_machine(prev) or not _on_a_machine(nxt):
                continue
            if nxt.start >= prev.end:
                continue                    # sequential; nothing to round
            cycle = _cycle_of(routings.get(prev.item_code), prev_seq)
            qty = float(getattr(prev, "qty", 0) or 0.0)
            if cycle <= 0.0 or qty <= 0.0:
                continue
            setup = max(0.0, float(prev.occupancy_min or 0.0) - qty * cycle)
            pieces = (_worked_before(prev, nxt.start) - setup) / cycle
            tol = _TOL_MIN / cycle
            if pieces < 1.0 - tol:
                rows.append(_rounding_row(
                    batch, prev, nxt, pieces,
                    "before a single whole piece of"))
            elif (abs(pieces - round(pieces)) > tol
                  and not _explained(prev, nxt, entries, config)):
                rows.append(_rounding_row(batch, prev, nxt, pieces, "into"))
    return rows


def _rounding_row(batch, prev, nxt, pieces, phrasing) -> dict:
    return {
        "kind": _KIND_ROUNDING,
        "ref": f"{batch}/{nxt.process_seq}",
        "message": (f"'{nxt.process_name}' of batch {batch} starts "
                    f"{pieces:.2f} pieces {phrasing} '{prev.process_name}'; a "
                    f"process can only start on a whole piece")}


def _cycle_of(routing, seq) -> float:
    if routing is None:
        return 0.0
    for proc in getattr(routing, "processes", None) or ():
        if int(proc.seq) == int(seq):
            return float(proc.cycle_time or 0.0)
    return 0.0


def _worked_before(entry, moment) -> float:
    """Minutes the machine actually worked this operation before ``moment``.
    Worked, not elapsed: an overnight gap cuts nothing."""
    total = 0.0
    for start, end in _covered(entry):
        if end <= moment:
            total += (end - start).total_seconds() / 60.0
        elif start < moment:
            total += (moment - start).total_seconds() / 60.0
    return total


def _explained(prev, nxt, entries, config) -> bool:
    """Something other than the release decides when ``nxt`` starts."""
    slack = timedelta(minutes=_TOL_MIN)
    key = shift_key(nxt.start, config)
    if key is not None:
        opened = _shift_bounds(key[0], key[1], config)[0]
        if abs(nxt.start - opened) <= slack:
            return True                     # the shop had just opened
    if not any(start <= nxt.start < end for start, end in _covered(prev)):
        return True                         # its feeder was not even cutting then
    who = next((w for _s, _e, w in _segments(nxt) if w), "")
    for other in entries:
        if other is nxt:
            continue
        for _s, end, worker in _segments(other):
            if abs(nxt.start - end) > slack:
                continue
            if other.machine == nxt.machine or (who and worker == who):
                return True                 # its machine / its operator was busy
    return False


# --------------------------------------------------------------------------- #
# All four
# --------------------------------------------------------------------------- #

def all_violations(entries, masters, config, absent=None) -> list:
    """Every violation in one list, in a fixed order: identical inputs give an
    identical list, because the optimizer builds thousands of plans and a report
    that reshuffles is a report nobody can diff."""
    entries = list(entries or ())
    if not entries:
        return []
    rows = []
    rows.extend(operator_split_violations(entries, config, masters))
    rows.extend(segmentation_violations(entries))
    rows.extend(idle_capacity_violations(entries, masters, config, absent=absent))
    rows.extend(overlap_rounding_violations(entries, masters, config))
    return rows
