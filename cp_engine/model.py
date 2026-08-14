"""The native PyJobShop layer: everything the library expresses well.

What is here: machines with calendar breaks, tasks, modes, the outsourcing pool,
and the precedences that do not depend on a decision. What is NOT here, and lives
in rules.py instead: Rule 1's roster, Rule 3's per-job overlap and Rule 4's setup
credit — the three things that need a variable where PyJobShop takes a constant.

WORKER-ONLY. This module imports pyjobshop, which is deliberately absent on
Render (see cp_engine/__init__.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from cp_engine import windows
from cp_engine.domain import DISPATCH, OUTSOURCED

# Enough parallel capacity that outsourcing never binds: a vendor takes every
# batch at once. The engine models an OS step as a flat 24x7 block with no
# operator and no calendar, and this is that.
_OS_HEADROOM = 1


@dataclass
class Built:
    """The model plus every index map the later layers address it by.

    PyJobShop's Task is not hashable and its Variables are keyed by INDEX, so
    the maps are the interface between this layer and rules/objective/genome.
    """

    m: object                       # pyjobshop.Model
    data: object                    # pyjobshop.ProblemData
    task_of: dict                   # (job_key, op_seq) -> task index
    job_of: dict                    # job_key -> job index
    # machine_res / operator_res hold the pyjobshop OBJECTS returned by
    # add_machine/add_renewable (add_mode needs the object, not an index) —
    # they are NOT index maps despite the name. Unhashable
    # (TypeError: unhashable type: 'Machine'), so they can never key a dict
    # or appear in a tuple used as one. The INDEX a later layer needs to
    # address Variables.assign_vars[(task_idx, resource_idx)] lives in
    # machine_res_order / operator_res_order below — use
    # machine_res_index()/operator_res_index(), never these two directly.
    machine_res: dict               # machine id -> pyjobshop Machine object
    operator_res: dict              # operator name -> pyjobshop Renewable object
    os_res: int
    shifts: list
    jobs: list
    dated_jobs: set                 # job keys that HAVE a delivery date
    # Resource indices of the CNC/VMC machines — the ONE machine-keyed answer to
    # "does this carry a 90-minute setup". It replaces an earlier
    # ``machining_tasks`` map keyed on the step's KIND, which was read by nothing
    # and could not have answered the question: a step's kind is taken from its
    # FIRST machine option (``domain._kind_for_machine_id``) and ``_candidates``
    # puts Allotted first, so ``MD1/CNC1`` is a manual-kind step that can run on a
    # CNC and ``CNC1/MD1`` a machining-kind one that can run on a bench.
    machining_res_idcs: frozenset = frozenset()
    setup_mode: str = "credit"

    # The wall clock the shift minutes are measured from. Carried so a later
    # layer can convert a Shift BACK to real time — operator absences arrive as
    # datetimes (engine.book_store stores {from_date, to_date}) and rules.py has
    # to test them against a shift, which is integer minutes.
    plan_start: datetime | None = None

    # Which encoding of Rule 2's "may span an unmanned shift" clause this data
    # was built FOR (spec §5.1). It is not merely a rules.py concern: E2 needs
    # allow_idle on the machining tasks (the held-but-unstaffed time is idle),
    # and allow_idle is frozen into ProblemData — Variables gives the idle var an
    # upper bound of 0 when it is False, and nothing downstream can relax it.
    # Recorded here so rules.add_roster can check the caller agrees rather than
    # silently building E2 constraints on a model that cannot satisfy them.
    hold_across_unmanned_shift: bool = True

    # The frozen in-progress pins this data was built FOR — {(job key, op seq):
    # rules.Pin}. Two of the four things a pin does are baked in HERE and cannot
    # be relaxed later: a resumed op's mode carries NO setup, and its task carries
    # the previous plan's start as ``earliest_start``. Recorded so
    # ``rules.pin_frozen`` can refuse a caller that forces a DIFFERENT set,
    # exactly as ``add_roster`` refuses a mismatched hold flag.
    pins: dict = field(default_factory=dict)

    # Rule 4's credit, as a SECOND mode. (task idx, machine res idx) -> mode idx
    # of a duplicate mode carrying the cutting time with NO setup, built only
    # where some other task of the same (item, process) can run on that same
    # machine. See ``_add_setup_free_modes`` for why the credit is a mode rather
    # than the IntVar subtracted from processing time that the plan described.
    #
    # ⚠ These modes are UNSAFE on their own: nothing here stops the solver from
    # selecting one, and an unlinked model silently invents 90 minutes of CNC
    # capacity per affected task. ``rules.add_setup_credit`` is what ties each to
    # "the machine's previous job was the same part", and any caller that builds
    # with ``setup_mode="credit"`` MUST call it — assert ``setup_credit_linked``
    # before solving, the way ``rules.add_roster`` checks
    # ``hold_across_unmanned_shift`` rather than trusting its caller.
    setup_free_modes: dict = field(default_factory=dict)
    # Set by ``rules.add_setup_credit``. False on a freshly built model, so
    # "were the credit modes constrained?" is one boolean a caller cannot forget
    # to ask. True even when there were no credit modes to constrain — it means
    # "safe to solve", not "did any work".
    setup_credit_linked: bool = False
    # (machine res idx, (item code, op seq)) -> the task indices that share it.
    # The ONE definition of "same part, same side, on a machine that could run
    # both" — rules.py groups the credit off this rather than re-deriving the
    # signature, or the two could drift and the credit would be granted for a
    # sameness model.py never grouped by. Every group has >= 2 members.
    setup_groups: dict = field(default_factory=dict)

    # Filled by later layers. Declared HERE so there is one definition of this
    # object's shape: rules.py populates shift_work (E2), addressing the model by
    # index through the maps above.
    shift_work: dict = field(default_factory=dict)     # task idx -> [IntVar] (Task 4)
    # (task idx, shift idx) -> IntVar. The overlap of a task with a shift depends
    # on nothing else, so it is built once and shared by every candidate machine.
    shift_overlap: dict = field(default_factory=dict)
    job_by_key: dict = field(default_factory=dict)     # job key -> domain.Job
    machine_res_order: dict = field(default_factory=dict)   # machine id -> res idx
    operator_res_order: dict = field(default_factory=dict)  # operator name -> res idx

    def machine_res_index(self, mid: str) -> int:
        """Resource index of a machine. PyJobShop's Variables are keyed by
        index — assign_vars is (task_idx, resource_idx) — and machines are added
        first, so a machine's resource index is its position in machine_res."""
        return self.machine_res_order[mid]

    def operator_res_index(self, name: str) -> int:
        """Resource index of a manual/inspection operator. Resources are
        indexed machines first, then operators (in the same sorted-by-name
        order build() creates them in), then the OS pool — mirroring
        machine_res_index so the two can never disagree about that ordering."""
        return self.operator_res_order[name]


def build(jobs, shop, config, plan_start: datetime, shifts,
          *, setup_mode: str = "credit",
          hold_across_unmanned_shift: bool = True, pins=None) -> Built:
    """``pins`` is ``rules.resolve_pins``'s output — already resolved against
    this book, so nothing here re-validates one. It changes exactly two things,
    both of which are frozen into ProblemData and unreachable afterwards: the
    resumed task's ``earliest_start``, and the absence of a setup on the machine
    the part is already in. It NEVER changes a quantity (2026-08-11)."""
    from pyjobshop import Model

    pins = dict(pins or {})
    horizon_min = shifts[-1].end if shifts else 0
    setup_min = int(getattr(config, "setup_time_min", 90) or 0)
    m = Model()

    machine_res, operator_res = {}, {}
    for mid, machine in sorted(shop.machines.items()):
        machine_res[mid] = m.add_machine(
            breaks=windows.machine_breaks(machine, shifts, horizon_min), name=mid)

    # Manual/inspection operators are ordinary capacity-1 renewables: Rule 1 does
    # not roster them, so which one runs a bench step is a free per-task choice.
    # Their SHIFT and their ABSENCES are not free, though, and nothing else in
    # the model carries those for a bench operator — Rule 1's roster covers
    # CNC/VMC only. They ride in as breaks on the renewable, from the same
    # windows helper the machine calendar comes from, so a shut station and a man
    # who is not there can never be read two ways.
    for operator in sorted(shop.operators, key=lambda o: o.name):
        operator_res[operator.name] = m.add_renewable(
            capacity=1,
            breaks=windows.operator_breaks(operator, shifts, horizon_min,
                                           shop.absent.get(operator.name, ()),
                                           plan_start),
            name=operator.name)

    os_res = m.add_renewable(capacity=max(1, len(jobs) + _OS_HEADROOM), name="OS")

    task_of, job_of, dated = {}, {}, set()
    # (machine id, item code, op seq) -> [(task object, task idx, cutting min)].
    # Two entries under one key are two sibling batches of the same part and side
    # that can meet on the same machine — the only shape Rule 4 ever credits.
    same_part: dict = {}
    for job in jobs:
        due_min = _due_minutes(job, plan_start)
        cp_job = m.add_job(due_date=due_min, name=job.key)
        job_of[job.key] = len(job_of)
        if due_min is not None:
            dated.add(job.key)

        prev_task = prev_op = None
        for op in job.ops:
            qty = job.qty_for(op.seq)
            if op.kind == DISPATCH:
                continue                      # a milestone, not work; §5.2
            if op.kind != OUTSOURCED and (qty <= 0 or not op.machine_options):
                continue

            # allow_idle is EXACTLY Rule 2's "the part stays in the chuck across
            # an unmanned shift": the machine is held, the clock runs, and
            # nothing is cut. Off under E1, where an operation may not span a
            # dark shift at all (spec §5.2).
            #
            # Keyed on the MACHINE, like the modes below and like the roster
            # itself — on whether this step can land somewhere Rule 1 rosters,
            # never on the step's own kind. A routing written MD1/CNC1 is a
            # manual-KIND step (the kind is read off the first option) that can
            # run on a rostered CNC: judged by kind it would be roster-covered
            # and yet forbidden to idle, so E2 — the encoding whose whole purpose
            # is to make such a plan possible — would return no plan at all.
            hold = hold_across_unmanned_shift and any(
                mid in shop.machining_ids for mid in op.machine_options)
            # A frozen op resumes no earlier than the previous plan started it —
            # the WHEN half of a pin (spec §5.5). ``earliest_start`` is a task
            # field, so it has to be set at creation; it is also what makes
            # ``rules._shifts_in_window`` tighten E2 for exactly this work.
            pin = pins.get((job.key, op.seq))
            task = m.add_task(job=cp_job, allow_breaks=True, allow_idle=hold,
                              earliest_start=(pin.earliest_start if pin else 0),
                              name=f"{job.key}/{op.seq}")
            idx = len(task_of)
            task_of[(job.key, op.seq)] = idx

            if op.kind == OUTSOURCED:
                m.add_mode(task, os_res, int(max(1, op.cycle_min)), demands=1)
            else:
                # The quantity is the BATCH's, pin or no pin. A frozen row's own
                # ``remaining_qty`` is one clubbed SO LINE's remainder and is
                # never read here — reading it planned 88 pieces of a 242-piece
                # step and left the rest in no plan at all (live 2026-08-11).
                cutting = max(1, int(round(qty * op.cycle_min)))
                for mid in op.machine_options:
                    # NO SETUP ON RESUME: the part is already in this chuck and
                    # the fixture is already on. Charged per MODE, so it is
                    # credited only on the machine the part is physically on —
                    # if the pin is later dropped the step pays setup wherever it
                    # does land.
                    resuming = pin is not None and mid == pin.machine
                    _add_modes(m, task, mid, cutting,
                               0 if resuming else setup_min, shop,
                               machine_res, operator_res)
                    # A pinned task is deliberately kept OUT of Rule 4's
                    # same-part groups: it already pays nothing, so it needs no
                    # credit, and it is not offered as a warm predecessor either.
                    # That over-charges at most one changeover behind a resumed
                    # op — the conservative direction, and the plan stays
                    # runnable.
                    if setup_min > 0 and pin is None and mid in shop.machining_ids:
                        same_part.setdefault(
                            (mid, job.item_code, op.seq), []).append(
                                (task, idx, cutting))

            if prev_task is not None:
                if prev_op.kind == OUTSOURCED or op.kind == OUTSOURCED:
                    m.add_end_before_start(prev_task, task)     # §5.3, sequential
                else:
                    # Rule 3's release is a VARIABLE lag, so it cannot be a
                    # StartBeforeStart(delay=...) here — rules.py adds it. Only
                    # the pacing half is expressible natively.
                    m.add_end_before_end(prev_task, task)
            prev_task, prev_op = task, op

    # Resources are indexed in creation order: machines first, then the manual/
    # inspection operators, then the OS pool. Recording that order here is what
    # lets rules.py address assign_vars[(task_idx, resource_idx)] without
    # re-deriving an index and getting it silently wrong. The operator order
    # uses the SAME sort key (by name) as the add_renewable loop above, so
    # operator_res_order can never drift out of step with what was actually built.
    order = {mid: i for i, mid in enumerate(sorted(shop.machines))}
    op_order = {op.name: len(machine_res) + i
                for i, op in enumerate(sorted(shop.operators, key=lambda o: o.name))}
    setup_free, setup_groups = (
        ({}, {}) if setup_mode != "credit"
        else _add_setup_free_modes(m, same_part, order, machine_res))
    return Built(m=m, data=m.data(), task_of=task_of, job_of=job_of,
                 machine_res=machine_res, operator_res=operator_res,
                 os_res=len(machine_res) + len(operator_res),
                 shifts=list(shifts), jobs=list(jobs),
                 dated_jobs=dated, setup_free_modes=setup_free,
                 setup_groups=setup_groups,
                 machining_res_idcs=frozenset(
                     order[mid] for mid in shop.machining_ids if mid in order),
                 setup_mode=setup_mode, machine_res_order=order,
                 operator_res_order=op_order,
                 job_by_key={j.key: j for j in jobs},
                 plan_start=plan_start, pins=pins,
                 hold_across_unmanned_shift=hold_across_unmanned_shift)


def _add_modes(m, task, mid: str, cutting: int, setup_min: int, shop,
               machine_res: dict, operator_res: dict) -> None:
    """Every way ONE machine can run this step, and what it costs there.

    Keyed on the MACHINE, never on the step's kind — for the crewing AND for the
    setup. Rule 1 is a property of people and the machines they are rostered to,
    and Rule 4 is a statement about the machine ("90 minutes on a CNC/VMC"),
    while a step's kind is only ever read off its FIRST machine option
    (``domain._kind_for_machine_id``). The two disagree the moment a routing
    lists ``MD1/CNC1`` or ``CNC1/MD1`` — and real routings do. A duration is per
    MODE and a mode is (task, machine), so "90 on a CNC, 0 on a bench, for the
    same step" is the shape PyJobShop already has.

    * A machine the roster covers (CNC/VMC) gets a **machine-only** mode carrying
      the setup: the man is whoever Rule 1 put on it for the shift. Booking one
      here as well would charge the same person twice for the same work, and a
      shop with one qualified operator would come out INFEASIBLE for work it can
      plainly do.
    * Every other machine carries its operator in the mode, because nothing else
      in the model will, and carries no setup, because a bench has no fixture to
      change.

    ``demands=[0, 1]``: a unary Machine takes no capacity and the operator takes
    one. ``[1, 1]`` is rejected as "infeasible demands".
    """
    if mid in shop.machining_ids:
        # Rule 4, inverted (§5.4): 90 minutes is ALWAYS in the duration and
        # credited back only for a same-part changeover — see
        # ``_add_setup_free_modes`` and ``rules.add_setup_credit``.
        m.add_mode(task, machine_res[mid], setup_min + cutting)
        return
    for name in _qualified(shop, mid):
        m.add_mode(task, [machine_res[mid], operator_res[name]],
                   cutting, demands=[0, 1])


def _add_setup_free_modes(m, same_part: dict, order: dict,
                          machine_res: dict) -> tuple:
    """A SECOND mode per (task, machining machine) carrying the cutting time
    alone — the shape Rule 4's credit takes.

    The plan called for an IntVar credit subtracted from the task's processing
    time. That cannot be built: PyJobShop already posts
    ``task.processing == mode.duration`` under the selected mode
    (``Constraints._select_one_mode``), so ``processing == duration - credit``
    forces the credit to zero and the model is INFEASIBLE for any other value
    (measured, not reasoned). A duration is per MODE, so a second mode is where a
    conditional duration belongs.

    Built ONLY for a (machine, item, process) that at least two tasks share —
    sibling batches of one item, which are rare — so the model is the same size
    as before everywhere else. Two batches of different items, the same item on
    its OTHER SIDE (a different process seq is a different fixture), or the same
    item on machines that cannot meet, generate nothing at all.

    Returns ``(setup_free_modes, setup_groups)``. The groups go out with the
    modes because they are the sameness the modes were built FOR; rules.py
    consumes them rather than re-deriving a signature of its own.
    """
    modes: dict = {}
    groups: dict = {}
    for (mid, item_code, op_seq) in sorted(same_part):
        rows = same_part[(mid, item_code, op_seq)]
        if len(rows) < 2:
            continue                    # nothing on this machine to change from
        res_idx = order[mid]
        groups[(res_idx, (item_code, op_seq))] = [idx for _t, idx, _c in rows]
        for task, task_idx, cutting in rows:
            modes[(task_idx, res_idx)] = len(m.modes)
            m.add_mode(task, machine_res[mid], cutting)
    return modes, groups


def _due_minutes(job, plan_start: datetime):
    """The last minute of the delivery DATE, or None.

    An order finishing any time ON its delivery date is on time, matching the
    app's ``(completion_date - due_date).days <= 0``. None for an undated order:
    pyjobshop asserts on a missing due date only when tardiness vars are built,
    and objective.py skips undated jobs for the same reason the roster engine
    does — recording 0.0 would claim a perfect landing.
    """
    if job.due is None:
        return None
    midnight = datetime.combine(job.due, datetime.min.time())
    return int((midnight - plan_start).total_seconds() // 60) + 1440


def _qualified(shop, mid: str) -> list:
    """Operators the Settings table says may run this machine.

    Qualification is EXACTLY the Settings machine list. Role is not a gate — it
    is inherited by name from the workbook's operator sheet, a fossil, and gating
    on it silently discarded the admin's assignment (2026-08-07: Sandeep Kumar
    was given CNC4, dropped from its pool as a workbook "helper", and CNC4 sat
    idle with work waiting).
    """
    return sorted(o.name for o in shop.operators
                  if mid in (getattr(o, "machines", None) or ()))
