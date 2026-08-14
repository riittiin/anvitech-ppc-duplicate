"""Total late-days first, then the most even distribution at that total.

    phase 1    minimise  sum_j L_j
    phase 2    minimise  sum_j D_j^2      subject to  sum_j L_j <= T* + eps

    where  L_j = true days late          (uncapped — the owner's headline number)
           D_j = min(L_j, CAP_DAYS)      (capped — the SQUARES, and only those)

WHICH METRIC GOES WHERE, AND WHY IT IS NOT INTERCHANGEABLE. The cap exists for
exactly one job: stopping one hopeless order's square from dominating the spread
and sending the search chasing an order it cannot save instead of the ones it
can. It has no business anywhere else, and letting it leak breaks the owner's
rule in a way that is invisible on the reported number:

* leaked into PHASE 1, an order already past sixty days late has a flat penalty,
  so delaying it further is FREE — the search will happily push it to buy a day
  somewhere cheaper, and the real total rises while the reported one does not.
* leaked into PHASE 2's constraint, the "fairness never costs a late-day"
  guarantee holds only on the capped metric. Phase 2 could push a capped order
  arbitrarily later to shave a square off another order, at zero cost to
  `sum D_j`, while the owner's real late-days climb.

Neither is theoretical: the soft cap exists precisely BECAUSE orders past sixty
days late are the normal state of this shop's book. So the no-regression
constraint and the headline number are both on L, and only the squares see D.

WHY SQUARED, AND WHY IT IS NOT ARBITRARY. With the total held fixed by phase 2's
constraint,

    Var(D) = (sum D_j^2)/n - ((sum D_j)/n)^2

and the second term is a CONSTANT. So minimising sum D_j^2 is EXACTLY minimising
the variance of tardiness across the book — the most even distribution
achievable at the best total. Ten orders ten days late scores 1,000; nine orders
two days plus one order eighty-two scores 6,760 at the identical total.

Preferred to max-tardiness (which pyjobshop offers natively) because max stops
discriminating once the worst order is pinned, while sum D^2 keeps spreading
everything below it. Preferred to a blended sum D + lambda*max D because the
trade-off would then live in an untunable lambda rather than in a stated rule.

eps DEFAULTS TO 0, so fairness can never cost a single late-day: it decides only
where phase 1 was indifferent. That keeps b7beb18 (2026-08-13, the incumbent
engine's on-time term made LINEAR so the score tracks total late-days) intact
rather than quietly reverting it.

Both are in DAYS, not minutes: it is the number the owner is judged on, it
matches the app's ``(completion.date() - due_date).days``, and it keeps the
squares small (0..60 -> 0..3600) so the chord encoding below stays tiny.

WORKER-ONLY **BY USE, NOT BY IMPORT.** It imports ``math`` and ``typing`` and
nothing else: every function here is handed the ``cp_model`` (an ortools
``CpModel``) as an ARGUMENT and calls methods on it. So importing this module on
Render — where ortools is deliberately absent (see ``cp_engine/__init__.py``) —
would succeed; it is worker-only because its only caller is ``cp_engine.solve``,
which does import the solver.

That distinction is recorded precisely because the solver-import boundary is this
package's load-bearing invariant: a false statement about which modules cross it
is how a replay-path module comes to be treated as untouchable, or a worker-only
one as safe to import. An earlier version of this line claimed "this module
imports ortools", which it never has.
"""

from __future__ import annotations

import math
from typing import NamedTuple

# The app's own on-time cap. One hopeless order must not swamp the plan and send
# the search chasing it instead of the orders it can still save.
#
# It is a cap on the SQUARE, and on nothing else — never on the completion, never
# on the headline total, never on phase 2's no-regression constraint. See
# ``add_days_late`` and the module docstring for what each leak costs.
CAP_DAYS = 60

_MINUTES_PER_DAY = 1440


class Lateness(NamedTuple):
    """One order's tardiness, in the two forms the two phases need.

    They are deliberately NOT interchangeable, and carrying them as one object is
    what stops a caller reaching for the wrong one: ``true`` is what the owner is
    judged on and what may never be traded away, ``capped`` exists only so one
    hopeless order's square cannot dominate the spread.
    """

    true: object            # uncapped days late — phase 1 and phase 2's cap
    capped: object          # min(true, CAP_DAYS) — the squares, and only those


def add_days_late(cp_model, variables, built) -> dict:
    """``{job key: Lateness(true, capped)}``, in whole integer days.

    Undated jobs get no variable at all: an order with no delivery date cannot be
    judged on-time or late, and recording 0.0 would claim a perfect landing.
    (``variables.tardiness_vars`` is not used for the same reason — pyjobshop
    builds it lazily and asserts that EVERY job has a due date.)

    **The cap is soft, and it has to be.** The obvious encoding is one variable
    with domain ``0..CAP_DAYS`` and ``D * 1440 >= end - due``. That is a HARD
    DEADLINE in disguise: it says no order may finish more than sixty days after
    its delivery date, and a book carrying anything older than that — the normal
    state of this shop's book — comes out INFEASIBLE, with no plan at all and
    nothing on screen to explain why.

    So ``true`` is sized off the closed horizon and can never bind, and
    ``capped = min(true, CAP_DAYS)``. Phase 1 minimises ``true``, so it sits
    exactly at ``ceil((end - due)/1440)``.

    ⚠ ``true`` is only pinned to that lower bound while it is IN the objective.
    Under phase 2 an order past the cap has a flat square, so its ``true`` is
    free to float anywhere the ``sum(true) <= T*`` budget allows. Read a solved
    order's days late from its COMPLETION, not from this variable
    (``solve._days_late_by_job``).

    An order finishing any time ON its delivery date is on time, because
    ``model._due_minutes`` puts ``due`` at the last minute of that date.
    """
    horizon = built.shifts[-1].end if built.shifts else 0
    out = {}
    for job in built.jobs:
        if job.key not in built.dated_jobs:
            continue
        job_idx = built.job_of[job.key]
        job_var = variables.job_vars[job_idx]
        due = built.data.jobs[job_idx].due_date

        # The most days late this job could possibly be, given the closed
        # horizon: enough that ``true`` is never the binding constraint, and no
        # more, so its domain stays as small as the horizon allows.
        ceiling = max(0, math.ceil((horizon - due) / _MINUTES_PER_DAY))
        true = cp_model.new_int_var(0, ceiling, f"late_{job.key}")
        cp_model.add(true * _MINUTES_PER_DAY >= job_var.end - due)

        capped = cp_model.new_int_var(0, CAP_DAYS, f"D_{job.key}")
        cp_model.add_min_equality(capped, [true, CAP_DAYS])
        out[job.key] = Lateness(true, capped)
    return out


def phase_one(cp_model, days: dict) -> None:
    """Minimise total late-days — the number on the Schedule tab, and the number
    the owner is judged on.

    On ``true``, never on ``capped``. Minimising the capped sum makes delaying an
    order already past sixty days FREE, so the search buys a day somewhere
    cheaper by pushing a hopeless order further out and the real total rises
    while the reported one does not.
    """
    cp_model.minimize(sum(d.true for d in days.values()) if days else 0)


def phase_two(cp_model, days: dict, total_star: int, slack_days: int = 0) -> None:
    """Minimise the spread, holding the UNCAPPED total at phase 1's result.

    The constraint is on ``true`` and the objective on ``capped``, and that split
    is the whole of the owner's rule. Constrain the capped sum instead and
    "fairness never costs a late-day" holds only on the capped metric: an order
    pinned at sixty could be pushed arbitrarily later to shave a square off
    another order, free on the reported number and expensive on the real one.

    ``slack_days`` is the eps: 0 makes phase 2 a strict tie-break that can never
    cost a late-day. It is a parameter rather than a constant so buying evenness
    is a config change if the owner ever asks for one, not a redesign.
    """
    if not days:
        cp_model.minimize(0)
        return
    cp_model.add(sum(d.true for d in days.values())
                 <= int(total_star) + int(slack_days))
    cp_model.minimize(sum(_square(cp_model, d.capped, f"sq_{i}")
                          for i, d in enumerate(days.values())))


def _square(cp_model, d, name: str):
    """``d^2``, exactly, for integer ``d`` in ``0..CAP_DAYS``.

    Linear lower-bounding lines rather than ``add_multiplication_equality``: the
    model stays linear, which CP-SAT propagates far better than a quadratic term.
    Minimisation pushes ``sq`` down onto the tightest line, and at integer d that
    line IS d^2.

    **They are CHORDS, not tangents, and that distinction is load bearing.**
    ``sq >= (2k+1)*d - k(k+1)`` is the straight line through ``(k, k^2)`` and
    ``(k+1, (k+1)^2)``. Its error is ``d^2 - line = (d - k)(d - k - 1)``, which is
    NEGATIVE for real d strictly between k and k+1 — so this is not a valid lower
    bound on the reals at all. It is valid here only because d is an ``IntVar``
    and an integer is never strictly between two consecutive integers.

    Being exact at BOTH ends is what sizes the loop: line k covers d = k and
    d = k+1, so ``k = 0..CAP_DAYS-1`` covers the whole domain ``0..CAP_DAYS`` and
    a ``CAP_DAYS``-th line would be redundant (measured: adding it changes
    nothing, dropping one more understates ``60^2`` as 3,598). 60 constraints per
    late order, ~4,000 on a 68-order book.

    Exact over the whole of d's domain BECAUSE ``add_days_late`` caps d at
    ``CAP_DAYS``: past the last chord the envelope falls below d^2 and the square
    would silently understate. The two constants are the same constant on purpose.
    """
    sq = cp_model.new_int_var(0, CAP_DAYS * CAP_DAYS, name)
    for k in range(0, CAP_DAYS):
        cp_model.add(sq >= (2 * k + 1) * d - k * (k + 1))
    return sq
