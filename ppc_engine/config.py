"""PlanConfig — the tunable knobs for one planning run.

Kept small and explicit. Objective weights live here so the *one* objective function
(objective/objective.py) reads them from a single place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for a single plan.

    Time knobs:
        plan_start:  The datetime the plan clock starts from (nothing is scheduled
                     before this). A concrete datetime keeps the engine pure — the
                     API is responsible for resolving "today" into this value, so the
                     engine never has to know the real clock (a lesson from the old
                     build's magic ``None`` = today convention).
        first_start / first_end:   First-shift clock times (default 08:00 → 19:00).
        second_start / second_end: Second-shift clock times (default 19:00 → 05:00
                     next day). ``second_end`` < ``second_start`` means it crosses
                     midnight.
        week_anchor: A Friday used as the reference for operator shift rotation.
                     Operators flip shift each Friday after this date. Defaults to the
                     Friday on/before ``plan_start`` (resolved by the caller).

    Scheduling knobs:
        setup_min:   Setup minutes charged once per *machining* operation (RULES.md).

    Objective weights (read by objective/objective.py):
        fairness_weight:  λ — RETAINED BUT UNUSED since 2026-08-06 (the on-time
                          term below subsumes the no-starvation guard it used to
                          express, RULES.md Rule 3); no longer read at all.
        makespan_weight:  w — weight on makespan (now a tie-break only).
    """

    plan_start: datetime

    # shift clock times
    first_start: time = time(8, 0)
    first_end: time = time(19, 0)
    second_start: time = time(19, 0)
    second_end: time = time(5, 0)

    # rotation reference (a Friday); caller resolves the default
    week_anchor: date | None = None

    # scheduling
    setup_min: float = 90.0

    # Order consolidation: merge same-item orders whose due dates fall within this many
    # days into ONE production batch (combined qty, one set of setups instead of two).
    # Saves setup time and can pull both deliveries earlier — but a bigger batch can
    # delay the earlier-due order, so the best window is book-specific and auto-tuned
    # (like overlap). 0 = no consolidation (each order produced separately). Default.
    # See engine/consolidation.py and OPTIMIZATION.md.
    consolidation_window: float = 0.0

    # How to pick which free operator mans a machine for a shift (RULES.md: "ideal —
    # one operator per machine per shift", plus the owner's idea of choosing by
    # flexibility/load rather than grabbing any free one):
    #   "scarce"   : the LEAST-flexible free operator (keeps flexible people free for
    #                machines only they can run — the measured old-build win). Default.
    #   "balanced" : the LEAST-loaded free operator (spread work evenly), tie → scarce.
    #   "flexible" : the MOST-flexible free operator (for contrast / A-B testing).
    operator_pick: str = "scarce"

    # Parallel batch splitting: run ONE operation on several of the machines its
    # routing allows, at the same time, each with its own operator for the whole
    # shift. Every extra machine costs another setup (setup_min), so a part is only
    # worth making if it carries real work — a 90-minute setup for ten minutes of
    # cutting is a loss. `split_setup_ratio` is that test: each part must carry at
    # least ratio x setup_min minutes of CUTTING. At the default 2.0 with a 90-min
    # setup, a 2-way split needs >= 6 hours of work in the operation.
    #
    # This is what `Config.split_parallel` / `split_min_qty` meant in the classic
    # engine; the new engine had no equivalent.
    #
    # DEFAULT OFF, and deliberately NOT wired to `Config.split_parallel`. Measured on
    # the live book (2026-08-12): splitting made the plan WORSE — 397 -> 401 late-days
    # even with the operator-slack guard (397 -> 421 without it). The reason is that
    # machine idleness there is not a machine problem: CNC3/CNC6/CNC7 are three
    # machines run by two day operators and one night operator, so their machine
    # utilisation is capped at 51% and already runs at 86% of the operator-hours that
    # exist. Splitting cannot create operator-hours; it only spends them faster and
    # starves the third machine.
    #
    # Kept, tested and off, because the calculus flips the moment a cell has more
    # hands than machines. Turn it on per-plan to re-measure.
    split_enabled: bool = False
    split_setup_ratio: float = 2.0
    split_max_ways: int = 3

    # Operation overlap (pipelining) — the floor-practical alternative to chunking.
    # An in-house op may START when its predecessor is this fraction through CUTTING
    # (setup excluded), instead of waiting for full completion. Each op still runs as
    # ONE continuous batch on ONE machine with ONE operator and ONE setup — nothing is
    # fragmented — so operator-machine stability is preserved. The successor is paced
    # so it never finishes before the predecessor (can't process pieces not yet made).
    # 0.0 = sequential (no overlap); 0.5 = start at 50% done; 0.9 = start at 90% done.
    # OS/dispatch never overlap. See scheduler/flow_scheduler.py and OPTIMIZATION.md.
    overlap: float = 0.0

    # RETAINED BUT UNUSED since 2026-08-06. `score()` no longer reads fairness_weight
    # or severity_*: the on-time term subsumes both. Kept (not deleted) so the field
    # names stay stable for anyone reading old plans or notes, matching how `pinned`
    # and `week_anchor` were retained inert when shift rotation was removed.
    fairness_weight: float = 30.0
    makespan_weight: float = 0.1

    # The on-time objective (2026-08-06 spec). ONE symmetric term replacing
    # total_tardiness + severity + the fairness term: for each order, how far it
    # misses its due date in EITHER direction, minus a free band, capped, squared.
    # Squaring spreads misses across orders instead of concentrating them, which is
    # the owner's stated requirement. The band is FLAT — no pull toward the exact
    # date. Must equal engine/optimizer.py ONTIME_* .
    ontime_band_days: float = 4.0
    ontime_cap_days: float = 60.0
    ontime_weight: float = 1.0

    # Reputation guard. RETAINED BUT UNUSED since 2026-08-06 (see the fairness_weight/
    # makespan_weight note above) — the on-time term now subsumes it, `score()` no
    # longer reads severity_*. Kept for the field names' stability. While it was
    # live, this was a CONVEX, capped per-order tardiness penalty that penalized
    # EVERY order's lateness on an accelerating curve. Must equal engine/optimizer.py
    # SEVERITY_* . cap=60 was the owner's "DISTRIBUTE THE PAIN" choice (2026-07-25):
    # measured on the real book, raising the cap 30->60 spread unavoidable lateness so
    # the worst orders dropped hard (SO108 53->26, SO107 47->23, plan worst 53->40)
    # instead of a few orders being catastrophically late while others sat on-time.
    # The trade the owner explicitly accepted: ~15 orders slid from on-time into
    # 5-13 days late (10 of them 10-13 d) so no order was left catastrophically worse
    # than it had to be. cap 60/90/120 gave identical plans (60 was the plateau).
    #   severity_tolerance_days (T): first T late days cost nothing extra.
    #   severity_weight (mu):        strength of the squared overage.
    #   severity_cap_days:           overage capped at this many days before squaring.
    severity_tolerance_days: float = 2.0
    severity_weight: float = 2.0
    severity_cap_days: float = 60.0

    # Worst-order ceiling barrier (2026-07-24 amendment). ceiling_days is the current
    # plan's worst lateness (days); the objective heavily penalizes any order pushed
    # PAST it, so re-optimization never worsens the worst order. None = no barrier
    # (byte-identical). Must equal engine/optimizer.py CEILING_WEIGHT.
    # MEASURED (Test5, 2026-07-24): at weight 100, with the ceiling set to the naive
    # worst (46 d), the search winner's worst order stays exactly at 46 and never
    # exceeds it (same at ceiling 61). The price on that book: holding the worst at 46
    # leaves no room to improve the rest (total/rescues unchanged) because the worst
    # order is on the critical path — so a re-optimize PROTECTS rather than churns
    # there; on a book with a genuine win-win (worst <= ceiling AND others better) it
    # still applies. Re-measure before moving.
    ceiling_days: float | None = None
    ceiling_weight: float = 100.0

    # Committed-promise breach term (mirrors the worst-order ceiling above, but
    # per-order against each order's own Order.promise_date rather than a single
    # plan-wide ceiling). committed_promise_slack_days: promise slips up to this
    # many days cost nothing extra. committed_promise_weight: strength of the
    # squared overage beyond the slack. 0 contribution when no order carries a
    # promise date (PlanMetrics.promise_slip_by_order is empty) — additive,
    # byte-identical otherwise.
    committed_promise_slack_days: float = 3.0
    committed_promise_weight: float = 5000.0
