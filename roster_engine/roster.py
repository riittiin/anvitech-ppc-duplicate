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
                     crew_rank: dict, prefer: dict | None = None) -> dict:
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

    matched = max_weight_matching(values, len(operators), len(machines))
    return {machines[c]: operators[r].name for r, c in matched.items()}
