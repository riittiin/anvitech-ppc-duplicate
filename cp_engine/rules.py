"""The shop rules PyJobShop cannot express, added to the raw CpModel underneath.

Rule 1 needs booleans per (operator, machine, shift). Rule 3 needs a VARIABLE lag
where StartBeforeStart takes a constant. Rule 4 needs the sequence literal that
says "t2 runs directly after t1". Only Rule 1 lives here so far.

Establishing why this file has to exist at all, rather than another few lines of
model.py: linking the roster to the work means saying "this machine does not work
during this shift, unless somebody is on it", and a break that DEPENDS ON A
DECISION is impossible in PyJobShop's own vocabulary. It pre-computes every
possible break duration per mode as a discrete choice keyed on start-time domains
(``Variables.BreakVar``, one per equivalence class of start time), so the break
set has to be known before the search begins. "This shift is unstaffed" is
exactly a thing the search decides.

WORKER-ONLY. This module imports ortools and pyjobshop, both of which are
deliberately absent on Render — nothing in the replay path (``__init__``,
``domain``, ``windows``, ``genome``, ``decode``, ``report``) may import it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from cp_engine import windows

# pyjobshop's own ceiling for a time variable. Imported rather than restated so
# an upper bound written here can never sit below one pyjobshop already allowed.
from pyjobshop.constants import MAX_VALUE


@dataclass
class Roster:
    """Who is on which machine, per shift.

    ``x`` holds a variable only for a pairing that is PHYSICALLY POSSIBLE — the
    machine is on the operator's Settings list, the shift is his shift, and he is
    not away. An impossible pairing is therefore absent from the model rather
    than forbidden by a constraint, which is both smaller and impossible to get
    wrong by forgetting to add one.
    """

    x: dict = field(default_factory=dict)        # (operator, machine, shift idx) -> Bool
    staffed: dict = field(default_factory=dict)  # (machine, shift idx) -> Bool


def add_roster(cp_model, variables, built, shop, *,
               hold_across_unmanned_shift: bool) -> Roster:
    """Rule 1: one operator mans exactly one machine for a whole shift.

    He may change machine only at the next shift, and only to a machine on his
    Settings machine list. **Binds CNC/VMC only** — helpers and inspectors
    physically walk between manual and inspection stations, and rostering them
    for a whole shift would delete capacity that really exists.

    Args:
        cp_model: the raw ``ortools.sat.python.cp_model.CpModel``, i.e.
            ``pyjobshop.solvers.ortools.CPModel.CPModel(...).model``.
        variables: that CPModel's ``.variables``.
        built: ``cp_engine.model.Built``.
        shop: ``cp_engine.domain.Shop`` — the Settings operator table.
        hold_across_unmanned_shift: which encoding of Rule 2's "may span an
            unmanned shift" clause to link the roster to the work with. See
            ``_link_work_to_roster``. Must match the flag ``built`` was built
            for; a mismatch raises rather than quietly building E2 constraints on
            a model that cannot satisfy them.
    """
    if bool(hold_across_unmanned_shift) != bool(built.hold_across_unmanned_shift):
        raise ValueError(
            "hold_across_unmanned_shift="
            f"{hold_across_unmanned_shift} but the model was built for "
            f"{built.hold_across_unmanned_shift}. E2 needs allow_idle on the "
            "machining tasks, which is frozen into ProblemData and cannot be "
            "relaxed here — pass the same flag to model.build().")

    roster = Roster()
    for mid in sorted(mid for mid in built.machine_res_order
                      if mid in shop.machining_ids):
        machine = shop.machines[mid]
        for shift in built.shifts:
            if not _machine_runs(machine, shift):
                continue                      # already a break for this machine
            here = []
            for operator in sorted(shop.operators, key=lambda o: o.name):
                if mid not in (getattr(operator, "machines", None) or ()):
                    continue                  # qualification == the Settings list
                if windows.operator_shift(operator) != shift.shift:
                    continue
                if _absent(shop, operator.name, shift, built):
                    continue
                var = cp_model.new_bool_var(
                    f"x_{operator.name}_{mid}_{shift.index}")
                roster.x[(operator.name, mid, shift.index)] = var
                here.append(var)

            flag = cp_model.new_bool_var(f"staffed_{mid}_{shift.index}")
            roster.staffed[(mid, shift.index)] = flag
            # One person per machine per shift, and the flag IS that person: an
            # equality, not an implication, so "staffed" can never be true with
            # nobody on it.
            if here:
                cp_model.add(sum(here) == flag)
            else:
                cp_model.add(flag == 0)

    # Rule 1 itself: nobody is on two machines in the same shift. He may change
    # machine at the next shift, which is why this groups on (person, shift) and
    # not on the person alone.
    per_person: dict = {}
    for (name, _mid, shift_idx), var in roster.x.items():
        per_person.setdefault((name, shift_idx), []).append(var)
    for _key, group in sorted(per_person.items()):
        if len(group) > 1:
            cp_model.add_at_most_one(group)

    _link_work_to_roster(cp_model, variables, built, roster.staffed,
                         hold=hold_across_unmanned_shift)
    return roster


# --------------------------------------------------------------------------- #
# Linking the roster to the work — the two encodings
# --------------------------------------------------------------------------- #

def _link_work_to_roster(cp_model, variables, built, staffed, *, hold: bool):
    """Work on a machining machine requires that machine to be staffed.

    E1 (hold=False) — dark-shift blocking. A shift with nobody on it BLOCKS the
    machine outright. Cheap (|M_c| x |S| intervals, no per-task variables) and
    restrictive: an operation may not SPAN an unstaffed shift, which Rule 2
    permits. It errs toward under-claiming capacity, so a plan built this way is
    always runnable.

    E2 (hold=True) — exact. Per (task, machine, shift) the processing minutes in
    that shift are a variable, capped at zero when the machine is unstaffed, so
    the part is HELD across an unmanned shift instead of being forbidden from
    spanning it.

    Which one ships is a measurement (spec §5.1, Task 1), not a preference, and
    both stay correct until it is made.
    """
    if not hold:
        _block_unstaffed_shifts(cp_model, variables, built, staffed)
        return
    _work_only_in_staffed_shifts(cp_model, variables, built, staffed)


def _block_unstaffed_shifts(cp_model, variables, built, staffed):
    """E1. An optional interval covering the shift, present iff unstaffed.

    It goes into a SECOND no-overlap of its own, alongside this machine's task
    intervals. CP-SAT fixes a NoOverlap's interval list when the constraint is
    created and there is no way to append to the one PyJobShop already built, so
    the choice is a second constraint or none. Two no-overlaps saying compatible
    things about the same intervals is redundant, not conflicting.
    """
    by_machine: dict = {}
    for (mid, shift_idx), flag in sorted(staffed.items()):
        shift = built.shifts[shift_idx]
        by_machine.setdefault(mid, []).append(
            cp_model.new_optional_interval_var(
                shift.start, shift.minutes, shift.end, flag.negated(),
                f"dark_{mid}_{shift_idx}"))

    for mid, dark in sorted(by_machine.items()):
        work = [assign.interval
                for assign in variables.res2assign(built.machine_res_index(mid))]
        cp_model.add_no_overlap(work + dark)


def _work_only_in_staffed_shifts(cp_model, variables, built, staffed):
    """E2. The processing minutes of a machining task, shift by shift.

    ``w[t,m,s] <= overlap(t, s)`` and ``w[t,m,s] <= len(s) * staffed[m,s]``, and
    the w's over a machine must cover the task's whole processing time when the
    task is assigned to it. The interval may then stretch across an unmanned
    shift — the machine is held, and that time is the task's ``idle``, which is
    why E2 needs ``allow_idle`` on the machining tasks and E1 does not.

    Note the sum is enforced PER MACHINE, under that machine's assignment
    literal, and only over machines the ROSTER covers. A routing may list a
    machining step's alternatives as CNC1/MD1 — the step's kind comes from the
    first option, so the task is machining while MD1 is not. Rule 1 does not bind
    MD1, so MD1 must carry no roster constraint at all: one sum across both
    machines, or a per-machine sum that treats MD1's empty w list as "cover your
    processing from nothing", would each quietly forbid a legal assignment.
    """
    rostered = {mid for (mid, _shift_idx) in staffed}
    for task_idx in sorted(built.machining_tasks):
        task_var = variables.task_vars[task_idx]
        shifts = _shifts_in_window(built, task_idx)
        for mid in _machines_for(built, task_idx):
            if mid not in rostered:
                continue                  # Rule 1 does not bind this machine
            res_idx = built.machine_res_index(mid)
            assign = variables.assign_vars.get((task_idx, res_idx))
            if assign is None:
                continue
            work = []
            for shift in shifts:
                flag = staffed.get((mid, shift.index))
                if flag is None:
                    continue      # a break for this machine: no work either way
                name = f"{task_idx}_{mid}_{shift.index}"
                overlap = _overlap_minutes(cp_model, task_var, shift, name)
                minutes = cp_model.new_int_var(0, shift.minutes, f"w_{name}")
                cp_model.add(minutes <= overlap)
                cp_model.add(minutes <= shift.minutes * flag)
                work.append(minutes)
            built.shift_work.setdefault(task_idx, []).extend(work)
            # An empty ``work`` is not a special case: the machine is open in no
            # shift this task could touch, and ``0 >= processing`` is false for
            # every machining task (90 minutes of setup are always in the
            # duration), so the same line rules the assignment out.
            cp_model.add(
                sum(work) >= task_var.processing
            ).only_enforce_if(assign.present)


def _overlap_minutes(cp_model, task_var, shift, name):
    """``min(end_t, end_s) - max(start_t, start_s)``, clipped at 0.

    A variable, not a constant: how much of a shift a task covers is exactly what
    the solver is deciding.
    """
    lower = cp_model.new_int_var(shift.start, MAX_VALUE, f"lo_{name}")
    cp_model.add_max_equality(lower, [task_var.start, shift.start])

    upper = cp_model.new_int_var(0, shift.end, f"hi_{name}")
    cp_model.add_min_equality(upper, [task_var.end, shift.end])

    overlap = cp_model.new_int_var(0, shift.minutes, f"ov_{name}")
    cp_model.add_max_equality(overlap, [upper - lower, 0])
    return overlap


# --------------------------------------------------------------------------- #
# Small shared predicates
# --------------------------------------------------------------------------- #

def _machine_runs(machine, shift) -> bool:
    """Does this machine work this shift at all?

    The same test ``windows.machine_breaks`` complements the calendar with, so a
    shift this returns False for is already a break and needs no roster: a
    single-shift station cannot be manned at night because it is not open at
    night, which is a different fact from nobody being available.
    """
    return bool(machine.is_two_shift()) or shift.shift == windows.FIRST


def _absent(shop, name: str, shift, built) -> bool:
    """Is this operator away for any part of this shift?

    Absences arrive as wall-clock blocks (``engine.book_store``'s
    ``{from_date, to_date}``, turned into 00:00 through 00:00-of-the-day-after)
    while a Shift is integer minutes, so the SHIFT is converted back to wall
    clock rather than the absence forward — one conversion, and it is then the
    same half-open overlap test (``away_from < shift_end and shift_start <
    away_to``) the sibling greedy engine's crew check applies, so the two can
    never disagree about who was away.
    """
    blocks = shop.absent.get(name) or ()
    if not blocks or built.plan_start is None:
        return False
    start = built.plan_start + timedelta(minutes=shift.start)
    end = built.plan_start + timedelta(minutes=shift.end)
    return any(away_from < end and start < away_to for away_from, away_to in blocks)


def _machines_for(built, task_idx) -> tuple:
    """The machine ids this machining task may run on, in a stable order.

    Read from the op's own ``machine_options`` — the list ``model.build`` created
    one mode per — so this cannot drift from what the model actually contains.
    """
    _job_key, op = built.machining_tasks[task_idx]
    return tuple(mid for mid in op.machine_options if mid in built.machine_res_order)


def _shifts_in_window(built, task_idx) -> list:
    """Only the shifts a task could possibly touch.

    This is the tightening E2's size rests on (spec §5.1: four constraints per
    (task, machine, shift) triple is tens of thousands otherwise). Today it bites
    only where a task carries a real window — frozen in-progress work, which pins
    ``earliest_start`` — and returns the whole horizon for everything else. That
    is honest rather than clever: the bound is read from the task PyJobShop
    actually built, so it tightens by itself as later layers narrow it, and can
    never claim a window the model does not have.
    """
    task = built.data.tasks[task_idx]
    lower = getattr(task, "earliest_start", 0) or 0
    upper = getattr(task, "latest_end", MAX_VALUE)
    return [s for s in built.shifts if s.end > lower and s.start < upper]
