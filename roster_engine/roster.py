"""Who mans which machine, for a whole shift. This is Rule 1.

An operator appears in at most one machine's roster for a shift, so "one operator
hopping between machines mid-shift" is not a thing this engine can express. The
live engine states the same rule in staffing.py's docstring and then implements a
"short-job exception" that books people per-minute; that is the defect this
package exists to remove.

Only CNC/VMC are rostered. A helper physically walks between deburring and
packing, and forbidding that would delete capacity that really exists.
"""

from __future__ import annotations

from roster_engine.assign import max_weight_matching
from roster_engine.worktime import machine_runs_shift, operator_shift

_EPS = 1e-9

# Continuing a part already in the chuck must beat any amount of raw demand
# elsewhere, or an operation would be segmented at every shift boundary. A shift
# is at most ~660 minutes, so this cannot be outweighed by real work.
CARRY_BONUS = 1_000_000.0

# How much one rank of the crew genome is worth, in "minutes of pending work".
# Big enough that the optimizer can genuinely move a decision; small enough that
# it can never man a machine with nothing to do. That guard is NOT arithmetic —
# a zero/negative-demand, non-carry pairing is never added to `values` at all
# (see the `pending <= 0.0 and mid not in in_progress: continue` below), so no
# value of LOOKAHEAD_UNIT could lift it above the "opt out" dummy column in
# max_weight_matching (which is always worth exactly 0). Kept well below
# CARRY_BONUS regardless, so it can never be mistaken for a carry-over.
LOOKAHEAD_UNIT = 45.0

# Sentinel default for a machine `crew_rank` says nothing about: sorts it after
# every real rank, whatever the real ranks happen to be (they need not be a
# dense 0..n-1 range). Used ONLY to order `machines`; the bias term below never
# touches `crew_rank` directly (see the comment there for why).
UNRANKED = float("inf")

# Continuity: the person the previous plan had on a part that is STILL in the
# chuck. Two orders of magnitude below LOOKAHEAD_UNIT and four below CARRY_BONUS,
# so it decides only between people who are otherwise weight-identical on the same
# machine — which, before this term existed, was every eligible person, and the
# matching then picked by row index. It can never man a machine with no work (a
# zero-demand, non-carry pairing is never added to `values` at all) and it is far
# too small to re-rank one machine against another. Deliberately a preference:
# qualification comes from Settings alone (live 2026-08-03), so a pinned operator
# the admin has since disqualified is simply not in `values` and loses outright.
PREFERRED_BONUS = 1.0


def eligible(operator, machine_id: str, window, shop) -> bool:
    """May this person man this machine in this shift?

    Qualification is EXACTLY the machine list the admin set in Settings. Role is
    not a gate: it is inherited by name from the workbook's operator sheet, a
    fossil, and gating on it silently discarded the admin's assignment (live
    2026-08-07 — Sandeep Kumar was given CNC4, dropped from its pool for being a
    workbook "helper", and CNC4 sat idle with work waiting).

    Does NOT check the working-day calendar — that is machine- and
    operator-independent, so `roster_for_shift` checks it exactly once, up
    front, rather than re-deriving the same answer here for every operator x
    machine pair.
    """
    if machine_id not in (getattr(operator, "machines", None) or ()):
        return False
    if operator_shift(operator) != window.shift:
        return False
    for start, end in shop.absent.get(operator.name, ()):
        if start < window.end and window.start < end:
            return False
    return True


def roster_for_shift(window, shop, demand: dict, in_progress: dict,
                     crew_rank: dict, prefer: dict | None = None,
                     ready: dict | None = None) -> dict:
    """Assign operators to CNC/VMC machines for ``window``.

    Args:
        window:      the shift being rostered.
        shop:        the Shop (machines, operators, calendar, absences).
        demand:      machine id -> minutes of work that could run on it this shift.
        in_progress: machine id -> job key of a part physically mid-run on it.
        crew_rank:   machine id -> rank (0 = first claim). The optimizer's lever;
                     empty means no bias.
        prefer:      machine id -> the operator the previous plan had on the part
                     that machine is holding. A tie-break worth PREFERRED_BONUS,
                     so a re-plan does not rename the person on a job that is
                     physically running; None/empty means no preference.
        ready:       machine id -> minutes of work STANDING at it right now (its
                     predecessors are done), as opposed to ``demand``, which also
                     counts work the routing walk projects will arrive later in
                     the shift. Manual and inspection benches are staffed from
                     whoever this matching does not take, so a bench with work in
                     this map must be left somebody who can do it — see
                     ``_match_reserving_benches``'s guard. None/empty falls back
                     to ``demand``, which is the CONSERVATIVE reading: a caller
                     that says nothing about what is standing has every machining
                     minute treated as standing, so the guard is at its strongest
                     and a reservation fires only where it demonstrably cannot
                     darken machining.

    Returns:
        {machine_id: operator_name}. A machine absent from the result is dark this
        shift, which is a true constraint, not a failure.
    """
    # Machine- and operator-independent, so it is one guard here rather than a
    # per-pair re-check inside `eligible` (n_operators x n_machines redundant
    # evaluations of the same answer).
    if not shop.calendar.is_working_day(window.day):
        return {}

    machines = sorted(
        (mid for mid in shop.machining_ids
         if machine_runs_shift(shop.machines[mid], window.shift)),
        key=lambda mid: (crew_rank.get(mid, UNRANKED), mid))
    operators = sorted(shop.operators, key=lambda o: o.name)
    if not machines or not operators:
        return {}

    n_ranks = len(machines)
    prefer = prefer or {}
    values = {}
    for r, operator in enumerate(operators):
        for c, mid in enumerate(machines):
            if not eligible(operator, mid, window, shop):
                continue
            pending = float(demand.get(mid, 0.0))
            if pending <= 0.0 and mid not in in_progress:
                continue                      # never man a machine with no work
            value = min(window.minutes, pending)
            if mid in in_progress:
                value += CARRY_BONUS
            # `c` is this machine's column index AFTER sorting by crew_rank, so
            # it is monotone in rank and, unlike a raw rank, always lands in
            # [0, n_ranks - 1] no matter how sparse or out-of-range the caller's
            # ranks are (a persisted crew genome from a bigger/different machine
            # set, or a shift that runs only some machines). n_ranks - c is
            # therefore always in [1, n_ranks] — strictly positive — so this
            # bias can never zero out or invert a genuine >0 pairing. The raw
            # rank could: `crew_rank.get(mid, n_ranks)` can exceed n_ranks,
            # making the old `n_ranks - crew_rank...` term go negative and
            # outweigh real pending work, dropping the pairing from the match
            # entirely (2026-08-12 review finding).
            value += LOOKAHEAD_UNIT * (n_ranks - c)
            if operator.name == prefer.get(mid):
                value += PREFERRED_BONUS
            values[(r, c)] = value

    standing = {mid: float((ready if ready else demand).get(mid, 0.0))
                for mid in shop.machines}
    matched = _match_reserving_benches(
        values, operators, machines,
        _bench_pools(window, shop, demand, standing, operators), standing)
    return {machines[c]: operators[r].name for r, c in matched.items()}


def _bench_pools(window, shop, demand: dict, standing: dict, operators) -> list:
    """``[(bench_id, pool of operator row indices, is_standing)]`` for every
    manual / inspection station with work this shift and somebody who could do
    it.

    Listed off ``demand`` — the forward-walk PROJECTION, the same figure the
    machining columns are valued from — because benches are staffed reactively
    during the shift while the matching is fixed at its start and Rule 1 locks
    whoever it takes for the whole window. Work arriving at a bench at 10:50 has
    to be reserved for at 08:00, exactly as a machine released by overlap at
    10:50 has to be manned from 08:00.

    ``is_standing`` says whether that work is physically waiting at the bench NOW
    (its predecessors are done) or is only projected to arrive. The two buy very
    different reservations — see ``_match_reserving_benches``. Measured, this is
    the whole difference between a fix and a trade: reserving for a PROJECTION as
    if it were standing work cost an already-working book 51 -> 54 late-days and
    3.29 -> 4.19 days of makespan, because a straight routing projects its bench
    work from the first shift while every piece is still on a CNC.

    Only stations this plan can actually staff are listed — ``shop.machines``
    membership and ``machine_runs_shift`` are exactly the two gates
    ``scheduler._run_shift`` applies when it builds the bench pools for real, so
    the roster reserves for a bench if and only if the scheduler would try to run
    one. A routing may name a machine the Machine master has not caught up with;
    the scheduler cannot open a bench for it either, so it is not a reason to
    take anybody off machining.

    Eligibility is ``eligible`` — the same Settings-qualification / shift /
    absence rule the matching itself uses. Reserving somebody who is on leave
    would leave the bench exactly as starved and cost a CNC for nothing.

    Sorted by machine id, and the pools are row indices into the already
    name-sorted ``operators``, so nothing here can leak set or dict iteration
    order into the plan.
    """
    pools = []
    for mid in sorted(demand):
        if mid in shop.machining_ids:
            continue
        machine = shop.machines.get(mid)
        if machine is None or not machine_runs_shift(machine, window.shift):
            continue
        if float(demand.get(mid, 0.0)) <= _EPS:
            continue
        pool = frozenset(r for r, o in enumerate(operators)
                         if eligible(o, mid, window, shop))
        if pool:
            pools.append((mid, pool, float(standing.get(mid, 0.0)) > _EPS))
    return pools


def _mans_standing_machining(matched: dict, machines, standing: dict) -> bool:
    """Does this roster still man a machining machine that has work STANDING at
    it? Vacuously true when no machining machine does."""
    if not any(standing.get(mid, 0.0) > _EPS for mid in machines):
        return True
    return any(standing.get(machines[c], 0.0) > _EPS for c in matched.values())


def _match_reserving_benches(values: dict, operators, machines,
                             bench_pools: list, standing: dict) -> dict:
    """The machining matching, constrained so a bench with work is never left
    with nobody who could man it.

    THE DEFECT THIS EXISTS FOR: only ``shop.machining_ids`` is matched over, and
    ``scheduler._floating_operators`` staffs the benches from whoever the
    matching did NOT take. So every cross-qualified person was absorbed onto a
    CNC/VMC, a bench was left with an empty pool, and ``_shift_demand``'s forward
    routing walk kept reporting machining demand for the step BEYOND that bench
    (it assumes upstream will be manned) — the same roster, shift after shift,
    until the horizon ran out and ``schedule`` raised. Measured: two operators
    each qualified on all seven machines were UNSCHEDULABLE where the same two
    people split specialised were fine, and DE-QUALIFYING one of them from the
    CNCs fixed it. Broadening a Settings assignment must never remove an option
    (2026-08-07: a person given an extra machine ended up with less work and the
    machine sat idle).

    This is a RESERVATION, not a lock. The reserved person is simply left out of
    the machining match; they join ``_floating_operators`` and are staffed onto
    benches PER INTERVAL, so one helper still serves several benches in a shift.
    Rule 1 — one operator, one machine, a whole shift — still binds CNC/VMC only,
    which is worth ~50% of late-days on books where helpers are scarcer than
    benches.

    It is a CONSTRAINT rather than a price on purpose. Pricing the bench into the
    matching (an extra column worth its demand) does not fix anything: bench
    cycle times are short, so a bench is almost always worth less than a CNC's
    backlog and loses every time — in the reproduction, MD1's 360 minutes against
    1,260 on each CNC. Coverage is not something the shop is willing to trade.

    A RESERVATION IS ONLY EVER WORTH WHAT THE BENCH'S WORK IS WORTH, and that is
    what the two strengths below are for. Both were measured; each on its own
    fails one half of the job:

      * a bench with work STANDING at it (its feeder is done, the pieces are
        physically there) takes the cheapest assigned person in its pool. This is
        what breaks the deadlock.
      * a bench whose work is only PROJECTED may take only somebody whose
        machining seat is ITSELF speculative — a machine with nothing standing at
        it either. Such a seat costs the plan nothing to vacate, so this
        reservation is free. Treating a projection as standing work instead cost
        an already-working book 3 late-days and 0.9 days of makespan (a straight
        CNC -> CNC -> bench routing projects bench work from the first shift,
        while every piece is still on a CNC). Refusing projections altogether
        costs the other half: a third generalist was locked to a CNC that had
        nothing standing either, and the deburring released mid-shift went
        unstaffed for a whole window.

    THE GUARD, on top of both: a reservation may never leave every machining
    machine that has work STANDING at it dark. Without it the fix simply moves
    the deadlock — one operator, one CNC, one bench: the bench's projected work
    takes the only operator, the CNC goes dark, and the work he was reserved for
    never arrives, because the CNC feeds it.

    ``standing`` is the caller's ``ready`` map, never the projection, on both the
    weak-reservation test and the guard: those are the two places that decide
    what may be GIVEN UP, and neither may be handed away on a promise.

    Who is given up: the assigned member of the bench's pool whose machining
    pairing is worth LEAST, ties by operator row (i.e. by name). Value ordering
    does the right thing for free — a carry-over pairing carries CARRY_BONUS, so
    a part physically in the chuck is the last thing surrendered, and the
    lookahead bias means the lowest-priority machine gives way first. It is a
    greedy pick, not the exact minimum-loss one (that would need a fresh matching
    per candidate); the re-solve after each bar lets the rest of the crew
    rearrange optimally around it, so the realised cost is usually smaller than
    the barred pairing's value.

    Terminates: a pass either applies at least one bar or returns, barred
    operators never come back, so there are at most ``len(operators)`` passes.
    """
    usable = values
    matched = max_weight_matching(usable, len(operators), len(machines))
    if not bench_pools:
        return matched

    barred: set = set()
    while True:
        progressed = False
        for mid, pool, is_standing in bench_pools:
            taken = set(matched)
            if not pool <= taken:
                continue                      # somebody who could man it is free
            if is_standing:
                candidates = pool
            else:
                # Only a speculative machining seat may be traded for projected
                # bench work — vacating it costs nothing that is happening.
                candidates = {r for r in pool
                              if standing.get(machines[matched[r]], 0.0) <= _EPS}
            if not candidates:
                continue
            trial_barred = barred | {min(
                candidates, key=lambda r: (usable[(r, matched[r])], r))}
            trial = {k: v for k, v in values.items() if k[0] not in trial_barred}
            candidate = max_weight_matching(trial, len(operators), len(machines))
            if not _mans_standing_machining(candidate, machines, standing):
                continue                      # would darken machining itself
            barred, usable, matched = trial_barred, trial, candidate
            progressed = True
        if not progressed:
            return matched
