"""The Optimize feature's sequence search (pure; no state, no UI, no I/O).

Rule 6 is a greedy, single-pass, non-delay scheduler: it builds exactly ONE plan,
and whichever operation is ready first claims the machine — so the same unlucky
orders can lose every near-race for weeks (measured on the real book: 723
order-days of queueing, 74% of it waiting for CNC/VMC). The batch sequence fed
into Rule 6 is worth days of makespan by itself, and one full plan evaluates in
well under a second — so instead of trusting one greedy pass, this module SEARCHES:
try a sequence, replay the unchanged Rule 6, score the plan, keep the best.

Design contract (see the 2026-07-13 optimize-plan spec):
  * Reuses the unchanged Rules 1→2→3 (once) and Rule 6 (per evaluation) — never
    duplicates rule logic.
  * Deterministic: same so_lines + config + masters + budget + seed → identical
    result, on any machine (budget counts EVALUATIONS, not wall-clock).
  * The result is a rank per composite order key "<SO No>\x1f<Item Code>";
    ``pipeline.apply_priority_rank`` replays it, and replaying reproduces exactly
    the metrics reported here (the "what you Apply is what you get" guarantee).
  * ``reserved`` (blocked machine/operator intervals — today: operator absences)
    is passed through to every Rule 6 evaluation, so those windows stay clear.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .pipeline import KEY_SEP
from .rules import rule1_consolidate, rule2_sort_by_date, rule3_tiebreak_process_time, \
    rule6_allocate

# Score = ONTIME_WEIGHT x ontime_breach + MAKESPAN_WEIGHT x makespan_days
#         (+ the two dormant guards: ceiling, committed-promise).
#
# 2026-08-06 (owner: "we care only about the deliveries-on-time thing"): makespan
# demoted 40 -> 0.1, a pure TIE-BREAK. It was 40 because on 2026-07-19 the goal was
# to minimize BOTH late days and schedule length; that note recorded the trade
# honestly — raising 10 -> 40 moved the live book from 78.4 d / 1327 late-days to
# 72.7 d / 1528, i.e. it bought 5.7 schedule days for 201 extra late-days. The goal
# is now on-time delivery only, which makes that trade the wrong way round. 0.1
# matches ppc_engine/config.py makespan_weight, so the two scorers finally agree.
MAKESPAN_WEIGHT = 0.1           # == ppc_engine makespan_weight

# The on-time objective (2026-08-06 spec, reshaped 2026-08-13). ONE symmetric term
# replacing total_late_days + slip_severity: for each order take how far it misses
# its delivery date in EITHER direction, ignore the first ONTIME_BAND_DAYS, cap the
# rest, and SUM it.
#
# It is LINEAR since 2026-08-13, at the owner's explicit request. It was SQUARED
# from 2026-08-06 — the mechanism he asked for by name, so that ten orders 6 days
# out (10 x 2^2 = 40) beat one order 30 days out ((30-4)^2 = 676). The measured
# consequence was that the search REFUSED plans which cut TOTAL late-days by
# concentrating them: a CP solver found one 86 late-days better than the live plan
# and the optimizer correctly rejected it. Correlation between this term and total
# late-days, on a loaded book: squared -0.61, linear +0.49. See
# `scripts/measure_linear_ontime.py` and roster_engine/objective.py for the A/B.
#
# The cap stops a single hopeless order swamping the plan. The band is FLAT by
# owner decision — no pull toward the exact date; anywhere inside +/-4 days is
# equally on time. The ceiling and committed-promise guards below keep THEIR
# squaring: they are no-regression barriers, not the objective.
#
# The three CONSTANTS must stay numerically EQUAL to ppc_engine/config.py ontime_*;
# the SHAPE deliberately differs from it now (ppc_engine is vendored, drives only
# the retired `new` engine, and still squares). tests/test_scorer_mirror.py pins
# both halves of that statement.
ONTIME_BAND_DAYS = 4.0          # == ppc_engine ontime_band_days
ONTIME_CAP_DAYS = 60.0          # == ppc_engine ontime_cap_days
ONTIME_WEIGHT = 1.0             # == ppc_engine ontime_weight

# REPORTING ONLY since 2026-08-06. `score()` no longer reads slip_severity — the
# on-time term above subsumes it (same squared shape, tolerance 2 -> 4, now
# two-sided). `plan_metrics` still COMPUTES and RETURNS slip_severity because it is
# a reported field: three tests assert it and removing a reported field is
# needless risk. Kept equal to ppc_engine/config.py severity_* .
SEVERITY_TOLERANCE_DAYS = 2.0   # == ppc_engine severity_tolerance_days (T)
SEVERITY_WEIGHT = 2.0           # == ppc_engine severity_weight (mu)
SEVERITY_CAP_DAYS = 60.0        # == ppc_engine severity_cap_days (owner "distribute the pain", 2026-07-25)

# Worst-order ceiling barrier (2026-07-24 amendment) — mirror of ppc_engine's
# ceiling term. == ppc_engine ceiling_weight. MEASURED on the real book (Test5,
# 2026-07-24): at weight 100 the search winner's worst order stays exactly AT the
# ceiling (46 d) and never above it, for ceilings 46 and 61 alike — so 100 holds
# the guarantee (the apply backstop is the ultimate safety net regardless).
# Re-measure before moving.
CEILING_WEIGHT = 100.0

# Committed-promise ceiling (2026-07-29) — mirror of the worst-order ceiling, but
# per-order: each committed order's ceiling is ITS promised_date + slack. MEASURED
# on Test8 (2026-07-29, ~half the book committed at its expected date, optimize
# budget 250): weight 5000 halved committed orders past promise+3 (8 -> 4) and cut
# the worst committed slip 16 -> 9 days, at ~2% more total late-days; weight 100
# helped less (8 -> 6). Re-measure on real committed patterns before moving.
COMMITTED_PROMISE_WEIGHT = 5000.0

# Local-search shape. Multi-start iterated local search: each restart hill-climbs a
# fresh random permutation until it has gone this many evaluations with no improvement,
# then a new restart explores a different basin. Smaller = more diverse restarts,
# larger = deeper per-basin refinement; tuned on the real book for the best final plan.
_RESTART_AFTER = 60        # non-improving evaluations before a fresh restart


@dataclass
class OptimizeResult:
    """What the search found. ``ranks`` is the persistable artifact."""

    ranks: dict = field(default_factory=dict)   # "<so>\x1f<item>" -> 1-based rank
    baseline: dict = field(default_factory=dict)  # metrics of the Rule-3 order
    best: dict = field(default_factory=dict)      # metrics of the best sequence
    evals: int = 0
    improved: bool = False
    cancelled: bool = False   # True when stopped early via should_cancel (best-so-far kept)
    # The roster engine searches a SECOND genome beside the sequence: {machine id ->
    # priority rank}, the order the roster fills machines in. It is as much a part of
    # the answer as ``ranks`` — replay the ranks with a different crew and you get a
    # different plan — so it is persisted by Apply alongside them. Empty for every
    # other engine, which has no such dimension.
    crew_rank: dict = field(default_factory=dict)


def score(metrics: dict) -> float:
    """Lower is better. ONE on-time term plus a makespan tie-break, and the two
    dormant guards.

    `ontime_breach` is the whole objective: squared distance from the delivery date
    in either direction, beyond a 4-day band, capped. It replaces total_late_days,
    slip_severity and the search's fairness term (2026-08-06 spec).

    Makespan is a TIE-BREAK at 0.1 — it separates plans that are otherwise equal and
    must never outrank a genuine on-time improvement.

    `ceiling_breach` and `committed_promise_breach` are unchanged and both dormant in
    production today; they do jobs the on-time term cannot express (no-regression
    across re-plans, and a different promised date).

    ``ontime_breach`` is REQUIRED, not ``.get``-defaulted: a metrics dict missing it
    would otherwise score as a PERFECT plan, and dicts arrive here from remote
    contest workers (engine/optimize_service.py). Fail loud, per CLAUDE.md. The
    two dormant guards below keep ``.get`` — absent means genuinely zero for them.
    """
    return (ONTIME_WEIGHT * metrics["ontime_breach"]
            + MAKESPAN_WEIGHT * metrics["makespan_days"]
            + CEILING_WEIGHT * metrics.get("ceiling_breach", 0.0)
            + COMMITTED_PROMISE_WEIGHT * metrics.get("committed_promise_breach", 0.0))


def makespan_days(schedule, plan_start) -> float:
    """The ONE makespan definition every surface must use: days from plan start
    (midnight) to the last operation's end, to two decimals. Reused by
    ``plan_metrics`` (the Optimize panel/header) and ``analytics`` (the Analytics
    tab) so those tabs can never report a different number for the same plan
    (cross-tab integration, 2026-07-26). Empty schedule -> 0.0."""
    from datetime import datetime
    last_end = max((e.end for e in schedule), default=None)
    if last_end is None:
        return 0.0
    t0 = datetime.combine(plan_start, datetime.min.time())
    return round((last_end - t0).total_seconds() / 86400.0, 2)


def expected_completion(schedule) -> dict:
    """The ONE expected-completion definition every surface must use: for each order
    ``(so_no, item_code)``, the DATE of the latest end across every schedule entry
    tied to it — including the OS / Outsourced and Off-machine lanes, because an
    order genuinely is not finished until its outsourcing and dispatch milestones are.

    A consolidated batch's entries carry every member's ``so_ref``, so each member is
    measured on the same ops; an SO carrying two item codes gets one date per item.

    Sibling of ``makespan_days``: shared so the Gantt, the Orders tab, the delay
    justification report and ``plan_metrics`` can never report different dates for the
    same plan (live 2026-08-07: the Gantt said 07-Sep, the delay report 04-Sep)."""
    out: dict = {}
    for e in schedule:
        d = e.end.date()
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            if k not in out or d > out[k]:
                out[k] = d
    return out


def plan_metrics(schedule, so_lines, plan_start, ceiling_days=None,
                 with_distribution=False, promise_slack_days=None) -> dict:
    """Owner-facing quality of one plan: makespan + lateness vs SO delivery dates.

    Each order (SO#, item) is judged by its OWN delivery date — a consolidated
    batch's schedule entries carry every member's so_ref, so members due earlier
    are correctly measured against their earlier date.

    ``with_distribution`` (only for the two plans SHOWN in the Optimize panel — never
    in the hot search loop) adds ``bands`` (order counts per lateness band) and
    ``worst_orders`` (the 20+ day ones — the shop-over-capacity list the owner acts on).

    ``promise_slack_days`` (optional) computes committed-promise breach and max slip
    for each committed order's ceiling = promised_date + slack.
    """
    due = {(l.so_no, l.item_code): l.delivery_date for l in so_lines}
    expected = expected_completion(schedule)
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    late = [g for g in gaps if g > 0]
    slip_severity = 0.0
    for g in gaps:
        over = g - SEVERITY_TOLERANCE_DAYS
        if over > 0:
            if over > SEVERITY_CAP_DAYS:
                over = SEVERITY_CAP_DAYS
            slip_severity += float(over * over)
    ceiling_breach = 0.0
    if ceiling_days is not None:
        for g in gaps:
            over = g - ceiling_days
            if over > 0:
                ceiling_breach += float(over * over)
    committed_promise_breach = 0.0
    max_committed_slip = 0
    if promise_slack_days is not None:
        promised = {(l.so_no, l.item_code): l.promised_date for l in so_lines
                    if l.commitment == "committed" and l.promised_date is not None}
        for k, pdate in promised.items():
            if k in expected:
                slip = (expected[k] - pdate).days       # calendar days past promise
                if slip > max_committed_slip:
                    max_committed_slip = slip
                over = slip - promise_slack_days
                if over > 0:
                    committed_promise_breach += float(over * over)
    # The on-time objective (spec 2026-08-06). `gaps` is SIGNED — negative is early —
    # and abs() is what makes early and late count the same, which is the owner's rule.
    ontime_breach = 0.0
    for g in gaps:
        over = abs(g) - ONTIME_BAND_DAYS
        if over > 0:
            if over > ONTIME_CAP_DAYS:
                over = ONTIME_CAP_DAYS
            # LINEAR since 2026-08-13 (was `over * over`) — see
            # roster_engine/objective.py for the measurement. The two scorers must
            # keep the same SHAPE or the contest picks winners on one yardstick and
            # reports on another.
            ontime_breach += float(over)
    result = {
        "makespan_days": makespan_days(schedule, plan_start),
        "late_orders": len(late),
        "total_late_days": int(sum(late)),
        "max_late_days": int(max(late)) if late else 0,
        "slip_severity": round(slip_severity, 2),
        "ceiling_breach": round(ceiling_breach, 2),
        "committed_promise_breach": round(committed_promise_breach, 2),
        "max_committed_slip": int(max_committed_slip),
        "orders": len(gaps),
        "ontime_breach": round(ontime_breach, 2),
    }
    if with_distribution:
        per = sorted(((k[0], k[1], (expected[k] - due[k]).days)
                      for k in expected if k in due), key=lambda t: -t[2])
        bands = {"on_time": 0, "d1_4": 0, "d5_9": 0, "d10_19": 0, "d20_plus": 0}
        for _so, _it, g in per:
            bands["on_time" if g <= 0 else "d1_4" if g <= 4 else "d5_9" if g <= 9
                  else "d10_19" if g <= 19 else "d20_plus"] += 1
        result["bands"] = bands
        result["worst_orders"] = [{"so": so, "item": it, "days": g}
                                  for so, it, g in per if g >= 20][:20]
    return result


def _work(batch, masters) -> float:
    # In-house machine work only (per-piece cycle x qty). OS turnaround / off-machine
    # steps are excluded — same rule Rule 3 and Rule 6 use — so an outsourced block
    # never masquerades as cycle x qty machining in the ATC seed. See
    # rule3_tiebreak_process_time._cycle_per_piece.
    r = masters.routings.get(batch.item_code)
    return rule3_tiebreak_process_time._cycle_per_piece(r) * batch.qty


def _atc_key(batch, masters, plan_start):
    """Apparent-Tardiness-Cost flavour: due-date pressure divided by work — the
    best single-pass dispatch rule found on the real book (a strong seed)."""
    slack_days = (batch.so_delivery_date - plan_start).days
    p = max(_work(batch, masters), 1.0)
    return -((1.0 / p) * math.exp(-max(slack_days, 0) / 14.0))


def ranks_for(seq) -> dict:
    """The persistable artifact: every member order of the i-th batch gets rank i+1
    (batch members share a rank; ``apply_priority_rank`` replays by min member rank)."""
    ranks: dict = {}
    for i, b in enumerate(seq):
        for so in (b.source_so_refs or []):
            ranks[f"{so}{KEY_SEP}{b.item_code}"] = i + 1
    return ranks


def optimize(so_lines, config, masters, *, reserved=None, budget_evals=150,
             seed=42, on_progress=None, should_cancel=None, frozen=None) -> OptimizeResult:
    """Search for a better batch sequence for THIS book (rolling: call it on
    whatever the order book holds today; the result is disposable and re-computable).

    "Better" = lower ``score`` (a symmetric on-time penalty for missing the delivery
    date in either direction, plus a makespan tie-break) — every active line
    competes in one pool (lanes are pure status labels, no scheduling effect).

    ``on_progress(evals_done, best_metrics)`` is called after every evaluation.
    ``should_cancel()`` (optional) is polled between evaluations; when it returns True the
    search stops early and returns the best plan found so far. Exceptions from Rule 6 are
    not caught: a book that cannot plan at all should fail loud exactly like Plan does.
    """
    # The new engine runs its own sequence search + objective; delegate there (so the cloud
    # contest, which calls this per overlap candidate, becomes the new engine's contest).
    if getattr(config, "scheduler", "classic") == "new":
        from engine import new_engine
        return new_engine.optimize_sequence(
            so_lines, config, masters, reserved=reserved, budget_evals=budget_evals,
            seed=seed, on_progress=on_progress, should_cancel=should_cancel, frozen=frozen)
    # The roster engine likewise owns its own search — over TWO genomes, the job
    # sequence and the crew. ``frozen`` MUST be forwarded: the fall-through body
    # below evaluates without it, which was safe only because classic and flow
    # ignore frozen by design. The roster adapter honours it, so an unforwarded
    # frozen set means the contest scores plans with in-progress work floating free
    # while the applied plan pins it — the ranks would answer the wrong question.
    if getattr(config, "scheduler", "classic") == "roster":
        from engine import roster_adapter
        return roster_adapter.optimize_sequence(
            so_lines, config, masters, reserved=reserved, budget_evals=budget_evals,
            seed=seed, on_progress=on_progress, should_cancel=should_cancel,
            frozen=frozen)
    config.validate()

    batches = rule1_consolidate.run(list(so_lines), config=config, masters=masters)
    batches = rule2_sort_by_date.run(batches, config=config, masters=masters)
    pri0 = rule3_tiebreak_process_time.run(batches, config=config, masters=masters)
    n = len(pri0)
    if n == 0:
        return OptimizeResult()

    evals = 0
    cancelled = [False]

    def _stop() -> bool:
        if cancelled[0]:
            return True
        if should_cancel and should_cancel():
            cancelled[0] = True
        return cancelled[0]

    from .pipeline import scheduler_for
    _sched_run = scheduler_for(config)

    def evaluate(seq) -> dict:
        nonlocal evals
        sched = _sched_run(list(seq), config=config, masters=masters,
                           reserved=reserved)
        evals += 1
        m = plan_metrics(sched, so_lines, config.plan_start_date)
        if on_progress:
            on_progress(evals, best_m if best_m and score(best_m) <= score(m) else m)
        return m

    best_seq, best_m = None, None      # GLOBAL best across every restart

    def consider(seq, m):
        nonlocal best_seq, best_m
        if best_m is None or score(m) < score(best_m):
            best_seq, best_m = list(seq), m

    # The Rule-3 order is the BASELINE (what we're trying to beat) — evaluate it first.
    baseline_m = evaluate(list(pri0))
    consider(pri0, baseline_m)  # baseline is always feasible -> best_m is set

    # Restart starting points: the strong dispatch heuristics first (SPT, ATC), then an
    # unlimited stream of fresh random permutations. A single trajectory from one seed
    # gets stuck in whatever local optimum it first descends into; independent restarts
    # from DIVERSE starts explore different basins and keep the global best, which
    # reliably finds better plans than one long run. Deterministic: every RNG is seeded.
    def _starts():
        yield sorted(pri0, key=lambda b: _work(b, masters))                       # SPT
        yield sorted(pri0, key=lambda b: _atc_key(b, masters, config.plan_start_date))  # ATC
        k = 0
        while True:
            s = list(pri0)
            random.Random(seed * 1000 + k).shuffle(s)
            k += 1
            yield s

    gen = _starts()
    restart = 0
    while evals < budget_evals and n >= 2 and not _stop():
        rng = random.Random(seed + 100000 + restart)   # per-restart move RNG
        cur_seq = list(next(gen))
        cur_m = evaluate(cur_seq)
        consider(cur_seq, cur_m)
        since_improve = 0
        # Hill-climb this restart until it stalls (then a fresh restart escapes it).
        while evals < budget_evals and not _stop():
            r = rng.random()
            cand = list(cur_seq)
            if r < 0.5 or n < 4:                                    # insertion
                i, j = rng.randrange(n), rng.randrange(n)
                b = cand.pop(i)
                cand.insert(j, b)
            elif r < 0.8:                                           # swap
                i, j = rng.randrange(n), rng.randrange(n)
                cand[i], cand[j] = cand[j], cand[i]
            else:                                                   # 3-batch block move
                i, j = rng.randrange(n - 3), rng.randrange(n - 3)
                blk = cand[i:i + 3]
                del cand[i:i + 3]
                cand[j:j] = blk
            m = evaluate(cand)
            if score(m) < score(cur_m) or (score(m) == score(cur_m) and rng.random() < 0.5):
                if score(m) < score(cur_m):
                    since_improve = 0
                cur_seq, cur_m = cand, m
                consider(cand, m)
            else:
                since_improve += 1
                if since_improve > _RESTART_AFTER:
                    break            # basin exhausted → next restart from a fresh start
        restart += 1

    return OptimizeResult(
        ranks=ranks_for(best_seq),
        baseline=baseline_m,
        best=best_m,
        evals=evals,
        improved=score(best_m) < score(baseline_m),
        cancelled=cancelled[0],
    )


# --------------------------------------------------------------------------- #
# Settings sweep — auto-tune the overlap % inside the same Optimize click
# (2026-07-15 spec; contract rewritten the same day after a live regression
# and two owner decisions). The rule is the simplest fair one: EVERY overlap
# candidate gets the SAME search depth and the best plan wins, full stop. The
# current setting runs first (an early Stop still leaves it the most-searched)
# and wins exact ties (no Settings churn) — but it has no depth advantage.
#
# Budget (owner cap, 2026-07-15): ONE button, ~1,000 plans total (~40 min on
# the 0.1-CPU Render instance), split EQUALLY across the contenders. The
# candidate list holds only settings that have ever been competitive: overlap
# 90/100 ranked last or next-to-last in every measured contest on both real
# books (9 tables; matches the 2026-07-13 finding that high overlap adds
# nothing once the sequence is optimized), so they are out — but a current
# setting outside the list is always added as a contender, so no saved config
# is ever excluded from its own contest. Measured vs the 6×400 full contest
# (2,400 plans): identical winner+plan on Test6@15-07, same winner within 16
# late-days on Test5@15-07, and on Test6@11-07 it picks 713 late-d/39.7 d
# where full depth picks 717/36.8 — equal-or-near outcomes at 42% compute.
# --------------------------------------------------------------------------- #
# 2026-07-19 (crew-smart scheduler): re-measured on the live 71-order book --
# 85/88 dominate (makespan 73.5-73.7 d vs 77.4 at 80, 79+ at 50/60); 50/60
# lost every contest and were dropped, 90/92 measurably worse than 85/88.
OVERLAP_CANDIDATES = (70, 80, 85, 88)

# Flow-mode contest (2026-07-19 flow-scheduler spec): the overlap % has no
# meaning without Rule 6's pacing, so the settings sweep tunes the chunk count
# instead — how many transfer chunks a batch flows between steps in. Measured
# on the live book: 4 best (49.6 d), 6 close, 1 = no pipelining (56.6 d).
# Flow evals are ~1.5s each (5x classic), so a contest must be SHORT enough to
# fit the cloud's 20-min window on 2 vCPU: 2 candidates run in ONE parallel round
# (~13 min wall for 400 evals each), 3+ candidates need a second round and time
# out. Chunk 4 is the reliable winner (43.7 d at depth), 6 the close runner-up;
# 1/2/3 lost every measured contest. So the lineup is exactly (4, 6).
FLOW_CHUNK_CANDIDATES = (4, 6)

# Local-fallback budget when flow times out of the cloud: on Render's 0.1 CPU a
# flow eval is ~15s, so a full 1000-eval search would run for HOURS. Cap it hard
# — a short search still yields the ~50 d / far-fewer-late-days class, a big win
# over classic, without hanging the free instance.
FLOW_LOCAL_BUDGET = 100

# The owner's overlap band for the roster engine (2026-08-12). Under this
# engine's definition (roster_engine.release) the percentage is the fraction of
# a step's pieces that must have CLEARED before its successor may start, so 50 =
# "start once half are done" and 100 = fully sequential. Below 50 a successor
# starts on a handful of pieces and the shop floor stops believing the plan, so
# the owner's band is the whole physically sane range. Six coarse points here;
# optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES is the finer grid the parallel
# cloud contest fans across Actions shards.
ROSTER_OVERLAP_CANDIDATES = (50, 60, 70, 80, 90, 100)


def knob_for(config):
    """The ONE setting the sweep contest tunes for this scheduler mode:
    ('flow_chunks', FLOW_CHUNK_CANDIDATES) under the flow scheduler,
    ('overlap_percent', ROSTER_OVERLAP_CANDIDATES) under the roster engine,
    ('overlap_percent', OVERLAP_CANDIDATES) under classic Rule 6 / the new engine.

    The roster branch is easy to miss because the KNOB NAME is the same as the
    default's: forget it and the contest still runs, still tunes overlap, and
    still looks right — it just searches classic's narrow (70, 80, 85, 88)
    instead of the owner's 50-100 band."""
    if getattr(config, "scheduler", "classic") == "flow":
        return "flow_chunks", FLOW_CHUNK_CANDIDATES
    if getattr(config, "scheduler", "classic") == "roster":
        return "overlap_percent", ROSTER_OVERLAP_CANDIDATES
    return "overlap_percent", OVERLAP_CANDIDATES


def sweep_contenders(current_overlap=None, candidates=OVERLAP_CANDIDATES):
    """The contest lineup: the current setting first (Stop-safety + tie
    privilege), then the remaining candidates in ascending order."""
    vals = [v for v in sorted(dict.fromkeys(candidates)) if v != current_overlap]
    return ([current_overlap] if current_overlap is not None else []) + vals


def sweep_total_evals(budget_evals, current_overlap=None,
                      candidates=OVERLAP_CANDIDATES):
    """Total evaluations one ``sweep_optimize`` call spends: the budget split
    equally across the contenders (integer division — the remainder is left
    unspent). The API sizes the progress display with this."""
    n = len(sweep_contenders(current_overlap, candidates))
    return (budget_evals // n) * n if n else 0


@dataclass
class SweepResult:
    """Winner of the (sequence, knob) search. ``result`` is the winning
    candidate's OptimizeResult (its ranks are the persistable artifact).
    ``overlap_percent`` carries the winning KNOB VALUE — the overlap % under
    classic Rule 6, the chunk count under the flow scheduler (``knob`` says
    which; the field keeps its historical name for wire compatibility)."""

    overlap_percent: int = 0            # the winning knob value (see docstring)
    knob: str = "overlap_percent"       # which config field the value belongs to
    flexible_machines: bool = False     # the winning machine-set (new engine)
    result: OptimizeResult = field(default_factory=OptimizeResult)
    table: list = field(default_factory=list)   # per-candidate probe outcomes
    evals: int = 0
    cancelled: bool = False
    crew_rank: dict = field(default_factory=dict)   # the winning crew genome (roster)


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None,
                   candidates=OVERLAP_CANDIDATES, base_reserved=None, frozen=None) -> SweepResult:
    """Search batch sequence AND overlap %. ``budget_evals`` is the TOTAL"""
    # The new engine owns its own search + objective; delegate there when it is the
    # selected scheduler (see engine/new_engine.py). The old sweep below runs only for the
    # retired classic/flow engines.
    if getattr(config, "scheduler", "classic") == "new":
        from engine import new_engine
        return new_engine.sweep_optimize(
            so_lines, config, masters, budget_evals=budget_evals, seed=seed,
            on_progress=on_progress, should_cancel=should_cancel, base_reserved=base_reserved, frozen=frozen)
    # Same for roster — and note ``frozen=`` below is NOT forwarded to
    # _sweep_optimize_classic, which has no such parameter. That is deliberate and
    # safe for classic/flow (they ignore frozen) and would be a silent, unnoticeable
    # defect for roster, which does not.
    if getattr(config, "scheduler", "classic") == "roster":
        from engine import roster_adapter
        return roster_adapter.sweep_optimize(
            so_lines, config, masters, budget_evals=budget_evals, seed=seed,
            on_progress=on_progress, should_cancel=should_cancel,
            base_reserved=base_reserved, frozen=frozen)
    return _sweep_optimize_classic(
        so_lines, config, masters, budget_evals=budget_evals, seed=seed,
        on_progress=on_progress, should_cancel=should_cancel,
        candidates=candidates, base_reserved=base_reserved)


def _sweep_optimize_classic(so_lines, config, masters, *, budget_evals=150, seed=42,
                            on_progress=None, should_cancel=None,
                            candidates=OVERLAP_CANDIDATES, base_reserved=None) -> SweepResult:
    """The original classic/flow overlap-sweep search (see history). ``budget_evals`` is the TOTAL
    budget for the whole contest; it is split EQUALLY across the contenders
    (the current setting + ``candidates``), ``budget_evals // n`` plans each.

    The fair-contest contract (owner decisions, 2026-07-15): every contender
    gets the SAME search depth and the best-scoring plan wins — the current
    setting has no depth advantage. It runs FIRST (an early Stop still leaves
    the user's own setting fully searched) and wins exact ties (no Settings
    churn), nothing more.

    ``base_reserved`` (optional) is merged into EVERY candidate's reservations
    (operator absences — physical unavailability). ``None`` (default) is a no-op.
    """
    from dataclasses import replace
    from engine.optimize_service import merge_reservations

    knob, default_cands = knob_for(config)
    if candidates is OVERLAP_CANDIDATES:
        candidates = default_cands       # caller took the default: follow the mode
    cur = getattr(config, knob)
    lineup = sweep_contenders(cur, candidates)
    others = lineup[1:]
    each = budget_evals // len(lineup)

    spent = 0
    cancelled = False
    table = []

    def _offset(base):
        if on_progress is None:
            return None
        def cb(evals, best_m):
            on_progress(base + evals, best_m)
        return cb

    def _run(ov, budget):
        """One seeded search under overlap ``ov``; returns None if cancelled."""
        nonlocal spent, cancelled
        if budget <= 0 or cancelled or (should_cancel and should_cancel()):
            cancelled = cancelled or bool(should_cancel and should_cancel())
            return None
        cfg = replace(config, **{knob: ov})
        reserved = merge_reservations(None, base_reserved) or None
        res = optimize(so_lines, cfg, masters, reserved=reserved,
                       budget_evals=budget, seed=seed,
                       on_progress=_offset(spent), should_cancel=should_cancel)
        spent += res.evals
        cancelled = cancelled or res.cancelled
        table.append({"overlap": ov, "eligible": True, "best": res.best,
                      "evals": res.evals})
        return res

    def _sc(res):
        """A comparable score, or None when the candidate never ran (cancelled:
        _run returned None)."""
        return score(res.best) if (res is not None and res.best is not None) else None

    # The current setting runs FIRST (an early Stop still leaves the user's
    # own setting fully searched), then every other contender gets the SAME
    # depth — a fair contest, no depth advantage for anyone.
    best_ov, best_res = cur, _run(cur, each)
    best_score = _sc(best_res)
    for ov in others:
        r = _run(ov, each)
        r_score = _sc(r)
        if r_score is None:
            continue
        # Best plan wins; an exact tie keeps the current setting (no churn).
        if best_score is None or r_score < best_score:
            best_ov, best_res, best_score = ov, r, r_score

    if best_score is None:      # every contender vetoed/cancelled/infeasible
        return SweepResult(overlap_percent=cur, knob=knob,
                           result=OptimizeResult(evals=spent, cancelled=cancelled,
                                                 best=None),
                           table=table, evals=spent, cancelled=cancelled)
    return SweepResult(overlap_percent=best_ov, knob=knob, result=best_res,
                       table=table, evals=spent, cancelled=cancelled)
