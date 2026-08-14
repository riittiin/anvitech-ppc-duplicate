"""Shared Optimize service — ONE code path for the settings-sweep contest,
used by BOTH the API's local compute (``api.main._start_optimize``) and the
GitHub Actions cloud worker (``scripts/cloud_optimize_worker.py``).

Pure: no storage, no HTTP. Callers hand in the book (orders + actuals), the
masters workbook bytes, and the saved config; the payload helpers serialize
exactly those via the models' own ``to_json``/``from_json``, so the worker
reconstructs the very objects the API uses and calls the very same functions.
With the fixed seed the search is deterministic — a cloud run of the same
contest is byte-identical to a local run (CLAUDE.md principle: planning logic
is never duplicated, and new surfaces must reuse the plan's own machinery).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import dataclass, field, replace

from engine import optimizer, orderbook
from engine.config import Config
from engine.loaders import load_all
from engine.models import Actual, Masters, Order

# The full fair contest the owner signed off on (2026-07-15): EVERY overlap
# contender at the same full depth — 6 candidates × 400 = 2,400 plans when the
# current overlap is one of the six. Only the cloud (GitHub Actions, 2 vCPU)
# can afford it; Render's 0.1-CPU instance runs the trimmed local fallback
# (optimizer.OVERLAP_CANDIDATES at 1,000 plans total).
# 2026-07-19: 50/90/100 dropped (lost every measured contest under the
# crew-smart scheduler); 85/88/95 added (the new winners' region).
CLOUD_OVERLAP_CANDIDATES = (60, 70, 80, 85, 88, 95)
# New engine: a FINE overlap grid (2–4% steps, dense around the sweet spot) so the parallel
# contest finds a near-continuous optimum (0.78/0.80/0.82…), not just coarse grid points —
# while still fanning out across the runner's cores (fast, unlike a sequential tuner).
CLOUD_NEW_OVERLAP_CANDIDATES = (60, 65, 70, 74, 78, 80, 82, 84, 86, 88, 90, 93)
# Flow-mode cloud contest: chunk counts (see optimizer.FLOW_CHUNK_CANDIDATES).
# Two candidates only: on 2 vCPU they run in ONE parallel round (~13 min wall
# for 400 evals each), fitting OPTIMIZE_CLOUD_TIMEOUT_MIN's default 20. Four
# candidates needed a second round and timed out (live 2026-07-19), falling back
# to the hours-long local path. Chunk 4 wins at depth, 6 is the close runner-up.
CLOUD_FLOW_CHUNK_CANDIDATES = (4, 6)
# Roster engine: a finer grid over the owner's 50-100 overlap band (see
# optimizer.ROSTER_OVERLAP_CANDIDATES for what the number means). 5% steps, 11
# shards — the same order as the new engine's 12-shard grid, which a measured
# live run finished in ~6.5 min, so it fits the cloud window with headroom.
CLOUD_ROSTER_OVERLAP_CANDIDATES = (50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100)


def cloud_candidates(config) -> tuple:
    """The cloud contest lineup for this config's scheduler mode.

    The full grid is always returned. Multi-seed search used to halve it to keep
    the job count fixed, on an estimate that a contest takes ~25 minutes against a
    40-minute watchdog. A measured live run does 24 jobs in **391.8 s** (~6.5 min,
    20 shards in parallel), so there is ample headroom to ADD seeds rather than
    trade for them — which is strictly better, since the grid measurement below
    shows overlap breadth is worth far less than seed breadth.
    """
    sched = getattr(config, "scheduler", "classic")
    if sched == "flow":
        return CLOUD_FLOW_CHUNK_CANDIDATES
    if sched == "new":
        return CLOUD_NEW_OVERLAP_CANDIDATES
    if sched == "roster":
        return CLOUD_ROSTER_OVERLAP_CANDIDATES
    if sched == "cp":
        # ONE job, not a candidate grid: the CP engine has no knob (see
        # optimizer.knob_for) — the release size k is in no objective, so k = 1 is
        # provably optimal and there is nothing to sweep (spec §5.3). NOT "the
        # solver tunes it per job"; that reading would suggest overlap tuning is
        # being handled somewhere, and it is not — it is maximum, always. The single
        # ``None`` is the candidate VALUE that ``contest_jobs`` and
        # ``run_candidate`` then carry through as "no knob to set"; an empty tuple
        # would produce an empty contest and silently search nothing.
        return (None,)
    return CLOUD_OVERLAP_CANDIDATES


CLOUD_BUDGET_PER_CANDIDATE = 400
# The new engine's decode is heavier, so its fine grid gets a smaller per-candidate budget
# (12 candidates × 150 ≈ 1,800 plans, ~15 min across 2 cores).
CLOUD_NEW_BUDGET_PER_CANDIDATE = 150


# Extra RNG seeds the cloud contest multi-starts from, on top of the base seed.
# EMPTY = off (one seed, exactly the pre-2026-08-12 contest).
#
# Enabling this does NOT cost more: cloud_candidates() halves the overlap grid in
# exchange, so the job count, plan budget and wall-clock are unchanged (pinned by
# TestCostInvariant). Seeds are traded for overlap breadth, never added on top.
#
# Extra RNG seeds the cloud contest multi-starts from, on top of the base seed.
#
# OFF. Multi-seed search was built, measured and switched off again (2026-08-12).
#
# The case for it looked strong in isolation. On the live book, 3 overlaps x 3
# seeds, each replayed at the overlap it was searched for, in late-days:
#
#     overlap   seed 42   seed 7   seed 99     best
#        78       403       402      371        371
#        86       365       389      365        365
#        93       374       366      394        366
#
#     spread of the BEST result per overlap (what overlap breadth buys) :  6
#     average spread between seeds at one overlap (what seeds buy)      : 28
#
# So seed choice looked worth ~5x overlap choice, and no seed was good everywhere.
# But those were SINGLE-CANDIDATE probes. In the real contest the winner is the
# best of 24 (or 72) candidates, and that maximum is already stable: on the live
# book a 2-seed contest (16,800 plans) and a 3-seed contest (50,400 plans) both
# returned exactly 389 late-days. Tripling the search bought nothing, because the
# full contest was already finding the best answer its objective can express.
#
# The lesson: variance between individual searches does NOT imply variance in the
# best-of-N. Keep this off unless the objective changes and re-opens the gap.
CLOUD_EXTRA_SEEDS: tuple = ()


def cloud_seeds() -> list:
    """Extra seeds for the cloud contest.

    OPTIMIZE_CLOUD_SEEDS (env, comma-separated ints) overrides the constant, so the
    allocation can be tuned from the Render dashboard without a deploy — the same
    pattern as OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE. Unset/blank/unparseable => the
    constant. Junk entries are skipped rather than failing a contest.
    """
    import os
    raw = os.environ.get("OPTIMIZE_CLOUD_SEEDS", "").strip()
    if raw:
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                continue                      # ignore junk; never break a contest
        if out:
            return out
    return list(CLOUD_EXTRA_SEEDS)


def cloud_budget(config) -> int:
    """Plans per candidate for the cloud contest, per scheduler mode.

    OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE (env, positive int) overrides both modes —
    the deep-compute knob (2026-08-01 Oracle-worker spec: 300 ≈ the measured −4.4%
    class on a 4-core box). Unset/invalid/≤0 → the mode defaults below.
    """
    import os
    # A CP solve is TIME-boxed (Config.cp_time_limit_sec), not eval-boxed:
    # cp_adapter.solve accepts ``budget_evals`` for signature symmetry and
    # IGNORES it, and its ``evals`` counts IMPROVED SOLUTIONS, not plans tried.
    # So a plan budget is not a number about this engine at all, and the env
    # override must not reach it either — 400 would size the Optimize progress
    # bar as "3 of 400" against a denominator that can never be reached. 1 = one
    # job, which is exactly what the contest runs.
    if getattr(config, "scheduler", "classic") == "cp":
        return 1
    raw = os.environ.get("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    return (CLOUD_NEW_BUDGET_PER_CANDIDATE
            if getattr(config, "scheduler", "classic") == "new"
            else CLOUD_BUDGET_PER_CANDIDATE)


def absence_reservations(absences):
    """Absence rows -> Rule 6 operator reservations: the person is 'busy'
    from 00:00 of from_date to 00:00 of the day AFTER to_date (inclusive)."""
    from datetime import datetime, date, timedelta
    res = {}
    for a in absences or []:
        try:
            f = date.fromisoformat(a["from_date"])
            t = date.fromisoformat(a["to_date"])
        except (KeyError, ValueError):
            continue                                   # malformed row — skip
        if t < f:
            f, t = t, f
        interval = (datetime.combine(f, datetime.min.time()),
                    datetime.combine(t + timedelta(days=1), datetime.min.time()))
        res.setdefault(a.get("operator", ""), []).append(interval)
    res.pop("", None)
    return res


def merge_reservations(a, b):
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, []).extend(v)
    return out


def reservations_from_schedule(schedule):
    """Machine id → busy intervals and operator name → busy intervals, computed
    from a plan's schedule. Currently unused (the planner is single-pass and
    lanes never reserve time) — kept deliberately as a general-purpose utility
    for turning a schedule into ``reserved=``-shaped blocked intervals. Moved
    here from api.main so the cloud worker shares it."""
    res = {}
    for e in schedule:
        m = e.machine or ""
        if m and "OS" not in m and "Off-machine" not in m and "Outsourced" not in m:
            res.setdefault(m, []).append((e.start, e.end))
        op = getattr(e, "operator", "") or ""
        if op:
            res.setdefault(op, []).append((e.start, e.end))
    return res


def book_signature(so_lines, absences=None, frozen=None):
    """Fingerprint of the BOOK state an optimization was computed on: which
    orders, how much work each still needs (headline + per-process), their
    lanes/promises, the operator absences, and any frozen (in-progress)
    operations. When production moves any of these, an applied optimization
    is stale — the auto trigger compares this signature. (Masters + settings
    are covered by api._inputs_signature.) ``frozen=None``/``[]`` produces the
    SAME signature as before frozen existed — the third element is only
    added to the blob when frozen is non-empty, so every pre-existing caller
    is byte-identical.

    Note: adding ``delivery_date`` (2026-08-04) changed the hash for every book,
    so the first auto-optimize after that deploy runs one contest it would
    otherwise have skipped. Harmless, and one-time."""
    rows = sorted(
        (l.so_no, l.item_code, round(float(l.qty), 3),
         json.dumps(l.process_qty or {}, sort_keys=True, default=str),
         getattr(l, "commitment", "open") or "open",
         str(getattr(l, "promised_date", None)),
         # 2026-08-04: a re-import can change the SO delivery date. Without it here
         # the auto trigger would call a date-only edit "nothing changed" and never
         # re-sequence around the new date.
         str(getattr(l, "delivery_date", None)))
        for l in so_lines)
    parts = [rows, sorted((a.get("operator", ""), a.get("from_date", ""),
                          a.get("to_date", "")) for a in (absences or []))]
    if frozen:
        parts.append(sorted(
            (f.get("so_no", ""), f.get("item_code", ""), f.get("op_seq"),
             f.get("machine", ""), round(float(f.get("remaining_qty", 0) or 0), 3))
            for f in frozen))
    blob = json.dumps(parts, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Payload — the book snapshot a cloud worker needs, serialized losslessly.
# --------------------------------------------------------------------------- #
def build_payload(orders: dict, actuals, masters_bytes, config: Config, *,
                  seed: int, candidates=CLOUD_OVERLAP_CANDIDATES,
                  budget_per_candidate=CLOUD_BUDGET_PER_CANDIDATE,
                  absences=None, operator_table=None, frozen=None,
                  seeds=None) -> dict:
    """Snapshot everything one contest depends on. JSON-safe. ``operator_table``
    (the app-owned {week_anchor, operators} dict) is carried verbatim — the
    worker applies the SAME as-of-effective-start rotation the API does, so a
    cloud run is byte-identical to a local one. ``frozen`` (a plain list of
    dict rows, same JSON-safe shape as ``absences``) carries the in-progress
    operations that must not be rescheduled."""
    return {
        "orders": [o.to_json() for o in orders.values()],
        "actuals": [a.to_json() for a in actuals],
        "masters_xlsx_b64": (base64.b64encode(masters_bytes).decode()
                             if masters_bytes else None),
        "config": config.to_dict(),
        "seed": seed,
        # Extra RNG seeds for the search to multi-start from. Omitted (None) =>
        # the contest runs at ``seed`` alone, exactly as it always did, so an
        # in-flight job from an older deploy is unaffected. See contest_jobs.
        **({"seeds": list(seeds)} if seeds else {}),
        "candidates": list(candidates),
        "budget_per_candidate": budget_per_candidate,
        "absences": list(absences or []),
        "operator_table": operator_table,
        "frozen": list(frozen or []),
    }


def parse_payload(payload: dict):
    """Rebuild (orders, actuals, masters, config, absences, operator_table,
    frozen) — the exact objects the API planned with, via the models' own
    from_json and the normal loader. ``operator_table`` is the raw stored
    dict (or None); the contest applies the as-of-effective-start rotation
    onto masters.operators. ``frozen`` is the last element."""
    orders = {}
    for d in payload["orders"]:
        o = Order.from_json(d)
        orders[o.key] = o
    actuals = [Actual.from_json(d) for d in payload["actuals"]]
    raw = payload.get("masters_xlsx_b64")
    if raw:
        _, masters = load_all(io.BytesIO(base64.b64decode(raw)))
    else:
        masters = Masters()
    config = Config.from_dict(payload["config"])
    config.validate()
    absences = list(payload.get("absences") or [])
    operator_table = payload.get("operator_table")
    frozen = list(payload.get("frozen") or [])
    return orders, actuals, masters, config, absences, operator_table, frozen


# --------------------------------------------------------------------------- #
# Contest setup — the pre-search work _start_optimize used to do inline.
# --------------------------------------------------------------------------- #
@dataclass
class ContestSetup:
    target: list = field(default_factory=list)   # the lines the search may reorder (ALL active)
    config: Config = None                        # effective (start advanced), expedite as saved
    search_config: Config = None                 # effective, expedite forced off
    # Operator absences: physical unavailability (not a promise reservation).
    # Lanes (open/committed) are pure status labels — they never reserve
    # time. ``absence_reserved`` is the raw ``absence_reservations(absences)`` dict.
    absences: list = field(default_factory=list)
    absence_reserved: object = None
    # The masters the contest must schedule against. Identical to the object
    # handed in UNLESS an operator table was supplied — then it is a shallow
    # copy carrying the app-owned operator table's shifts (rotation removed
    # 2026-08-05; the cached masters object is never mutated). ALL callers plan
    # against ``setup.masters``.
    masters: object = None
    # In-progress operations that must not be rescheduled (a plain list of
    # dict rows, same JSON-safe shape as ``absences``).
    frozen: list = field(default_factory=list)


def prepare_contest(orders: dict, actuals, masters, config: Config,
                    absences=None, operator_table=None, frozen=None) -> ContestSetup:
    """Everything the sweep needs, from the raw book. Every active line competes
    in ONE pool — lanes (open/committed) have no scheduling effect. Only
    operator absences reserve time. Raises ValueError when there is nothing to
    optimize (no active orders with work remaining).

    ``operator_table`` (the app-owned {week_anchor, operators} dict) overrides
    ``masters.operators`` with the shifts on file for each operator — every
    operator's shift holds every week until an admin changes it in Settings
    (automatic rotation was removed 2026-08-05) — applied onto a SHALLOW COPY
    so the caller's (cached) masters object is never mutated."""
    ab = absence_reservations(absences)
    so_lines = orderbook.active_so_lines(orders, actuals, masters)
    eff = orderbook.effective_plan_start_date(actuals, config.plan_start_date,
                                              masters.calendar)
    if eff != config.plan_start_date:
        config = replace(config, plan_start_date=eff)

    if operator_table:
        from engine.operator_master import operators_as_of
        masters = replace(masters, operators=operators_as_of(operator_table, eff))

    target = so_lines
    if not target:
        raise ValueError("nothing to optimize: no active orders with work remaining")

    # The batch sequence only has leverage when Expedite is off (see the
    # 2026-07-13 Expedite↔Optimize fix) — search in the pure non-delay model.
    search_config = replace(config, expedite_window_min=0)

    return ContestSetup(target=target, config=config, search_config=search_config,
                        absences=list(absences or []), absence_reserved=(ab or None),
                        masters=masters, frozen=list(frozen or []))


# --------------------------------------------------------------------------- #
# The contest itself — per-candidate runs + the shared winner rule.
# --------------------------------------------------------------------------- #
def pick_winner(current_overlap, current_flexible, rows, base_seed=None):
    """Best score wins; an exact tie keeps the current (overlap, machine-set, seed).

    ``base_seed`` is the payload's own seed. Including it in the tie-break means a
    second seed that merely EQUALS the incumbent never displaces it — same
    no-churn rule the overlap dimension already has. Rows from an older worker
    carry no ``seed`` key and simply never win a tie.

    A row is eligible to WIN only if its ``best`` is a plan ``score`` can rank
    (``optimizer.scoreable``). ``is None`` was the old test and it was the wrong
    one: a candidate that found nothing carries ``OptimizeResult.best``'s default
    EMPTY DICT, which is not None — so an empty candidate could be elected winner
    and then blow up in ``score`` (the 2026-08-15 'ontime_breach' failure). A
    contest where NO candidate produced a plan returns None, exactly as one where
    every candidate was cancelled does, and the caller reports that.
    """
    def _is_current(r):
        return (r.get("overlap") == current_overlap
                and bool(r.get("flexible")) == bool(current_flexible)
                and (base_seed is None or r.get("seed") == base_seed))
    ordered = sorted(rows, key=lambda r: (not _is_current(r), r.get("overlap")))
    best = None
    for r in ordered:
        if not r.get("eligible") or not optimizer.scoreable(r.get("best")):
            continue
        if best is None or optimizer.score(r["best"]) < optimizer.score(best["best"]):
            best = r
    return best


def _cp_genome_json(g) -> dict:
    """A CP genome in a shape ``json.dumps`` accepts, or ``{}``.

    ``cp_engine.genome`` is the REPLAY path — it imports neither pyjobshop nor
    ortools, directly or transitively (see that package's ``__init__``) — so this
    module may import it, and the cloud worker (which has the solver) certainly
    may. The import is local anyway so that a non-cp contest never pays for it.
    """
    if not g:
        return {}
    from cp_engine.genome import as_json
    return as_json(g)


def run_candidate(payload: dict, overlap: int, flexible: bool = False, *, on_progress=None,
                  should_cancel=None, seed=None) -> dict:
    """One contender, fully self-contained (safe to run in a subprocess): it
    rebuilds the book from the payload and searches every active line as one
    pool (lanes have no scheduling effect). ``reserved=`` is only the operator
    absences (physical unavailability). ``flexible`` selects the machine set
    (Allotted-only vs Allotted+Suggested — see ``Config.flexible_machines``).
    Returns a sweep-table row (+ ranks for the winner)."""
    orders, actuals, masters, config, absences, operator_table, frozen = parse_payload(payload)
    # The new engine loads its masters from the workbook; the cloud worker has no store, so
    # feed it the payload's workbook bytes directly (harmless for classic/flow).
    if getattr(config, "scheduler", "classic") == "new":
        from engine import new_engine
        _raw = payload.get("masters_xlsx_b64")
        new_engine.set_masters_bytes(base64.b64decode(_raw) if _raw else None)
    # "roster" and "cp": nothing to do here, deliberately. engine/roster_adapter.py
    # and engine/cp_adapter.py both read the Masters OBJECT that prepare_contest
    # builds below (build_jobs/build_shop take masters), never the workbook bytes,
    # so a cloud run needs no masters priming. Recorded explicitly because an
    # omission and a forgotten branch look identical at runtime.
    setup = prepare_contest(orders, actuals, masters, config, absences=absences,
                            operator_table=operator_table, frozen=frozen)
    knob, _cands = optimizer.knob_for(setup.search_config)
    cfg = replace(setup.search_config, flexible_machines=bool(flexible))
    # ``knob`` is None under "cp" — there is nothing to set, and ``overlap`` is the
    # ``None`` cloud_candidates emitted. Both ``**{None: ...}`` and ``int(None)``
    # raise, so the guard is not cosmetic: without it the first candidate of the
    # first cloud contest after the cutover dies.
    if knob:
        cfg = replace(cfg, **{knob: int(overlap)})
    res = optimizer.optimize(setup.target, cfg, setup.masters,
                             reserved=setup.absence_reserved,
                             frozen=setup.frozen,
                             budget_evals=int(payload["budget_per_candidate"]),
                             seed=int(payload["seed"] if seed is None else seed),
                             on_progress=on_progress, should_cancel=should_cancel)
    return {"overlap": int(overlap) if knob else None, "flexible": bool(flexible),
            "seed": int(payload["seed"] if seed is None else seed), "eligible": True,
            "best": res.best, "evals": res.evals, "ranks": res.ranks,
            # The CP engine's decision genome. Same argument as ``crew_rank``
            # below and it is the stronger case: a row is ALL the app ever sees of
            # a cloud candidate, and applying ranks WITHOUT the genome makes the
            # decoder fall back for every single operation — a well-formed plan
            # that nobody searched. {} for every other engine.
            #
            # FLATTENED HERE, and that is not decoration. ``cp_adapter.solve``
            # returns the genome as the solver built it, and four of its eight
            # maps are keyed by a 2-TUPLE. A row is the worker's RETURN hop:
            # ``scripts/cloud_optimize_worker.py`` ``json.dumps``es a body
            # containing these rows for both ``/optimize/shard-result`` and
            # ``/optimize/result``, and ``json.dumps`` REFUSES a tuple key
            # ("keys must be str, int, float, bool or None"). The worker's blanket
            # ``except Exception`` turns that into an error post, so the app
            # reports "solve worker not reachable" — false; it was reachable, and
            # it did solve. Without this, no path can produce a stored genome.
            # ``as_json`` is idempotent where ``to_json`` is not.
            "genome": _cp_genome_json(getattr(res, "genome", None)),
            # The roster engine's second genome. A row is ALL the app ever sees of a
            # cloud candidate, so without it here the winning ranks would be applied
            # and then replayed against a different roster — a plan nobody searched.
            # {} for every other engine (they have no crew dimension).
            "crew_rank": dict(getattr(res, "crew_rank", None) or {}),
            "cancelled": res.cancelled}


# Subprocess plumbing: a plain module-level initializer + runner so the pool
# works under both fork (Linux runner) and spawn (macOS dev) start methods.
_POOL = {"counter": None, "stop": None}


def _pool_init(counter, stop):
    _POOL["counter"], _POOL["stop"] = counter, stop


def _pool_run(args):
    payload, overlap, flexible, seed = args
    last = {"evals": 0}

    def cb(evals, _best):
        delta, last["evals"] = evals - last["evals"], evals
        c = _POOL["counter"]
        if c is not None:
            with c.get_lock():
                c.value += delta

    stop = _POOL["stop"]
    return run_candidate(payload, overlap, flexible, seed=seed, on_progress=cb,
                         should_cancel=(lambda: bool(stop.value)) if stop else None)


def contest_seeds(payload: dict) -> list:
    """The RNG seeds this contest searches from, base seed FIRST.

    The search is an iterated local search, so its answer depends on the random
    stream: on the live book at a fixed overlap, three seeds gave 389/365/365
    late-days. Searching several and keeping the best turns that luck into a
    choice. No ``seeds`` in the payload => just the base seed, i.e. exactly the
    contest that ran before this existed.
    """
    base = int(payload["seed"])
    out = [base]
    for s in payload.get("seeds") or ():
        if int(s) not in out:
            out.append(int(s))
    return out


def contest_jobs(payload: dict) -> list:
    """The ordered (overlap, flexible, seed) candidate list a contest evaluates —
    the SINGLE source of truth for run_contest AND the sharded worker, so they can
    never drift. Order: seed outer, machine-set, overlap inner. The BASE seed's
    whole sweep therefore runs first, so an early "Stop & keep best" still has the
    current settings fully searched."""
    config = Config.from_dict(payload["config"])
    # None under "cp" (no knob) — knob_value is the one expression that survives
    # that; `getattr(config, None)` raises.
    cur_value = optimizer.knob_value(config)
    if getattr(config, "scheduler", "classic") == "cp":
        # ONE candidate, spelled out — cp has no knob because its release size k
        # is in no objective (k = 1 always, spec §5.3), not because the solver
        # tunes overlap per job. sweep_contenders(None, [None]) drops the
        # sole `None` on both of its filters and returns [], which would make the
        # whole contest an EMPTY list of jobs — a deep search that reports done in
        # milliseconds, having searched nothing, with no error anywhere.
        contenders = [None]
    else:
        contenders = optimizer.sweep_contenders(cur_value, payload["candidates"])
    # The machine-set dimension only affects the new engine (flexible_machines is
    # inert for classic/flow — see engine/new_engine._new_masters). Gate the outer
    # loop on scheduler so classic/flow cloud contests stay single-pass and byte-
    # identical to their local counterpart (Task 3 established this same gate in
    # engine/optimizer.sweep_optimize / engine/new_engine.sweep_optimize).
    #
    # "roster" JOINED this dimension on 2026-08-13. It was single-pass on the
    # reasoning that it resolves machine options from the routing and searching the
    # Allotted/Suggested axis would double every run for nothing. The live book
    # disproved that: all 86 (item, process) pairs on CNC/VMC were pinned to ONE
    # machine, CNC3/CNC6/CNC7 carried 1,053 h across the three most loaded operators
    # in the shop, and CNC4 (48 h) / CNC5 (17 h) sat nearly idle WITH qualified
    # operators free. Those cells share no people, so widening the option set moves
    # work to a different machine AND a different crew. Cost: the job count doubles
    # (11 overlaps -> 22 jobs), which is two rounds of the workflow's 20 shards.
    #
    # "cp" is deliberately NOT in that list: under the CP engine the machine per
    # operation is a MODEL VARIABLE (cp_engine.domain always offers Allotted ∪
    # Suggested and the solver chooses), so doubling the contest buys nothing at
    # all and costs two rounds of the workflow's 20 shards — on an engine whose
    # single named risk is tractability.
    machine_sets = ((False, True)
                    if getattr(config, "scheduler", "classic") in ("new", "roster")
                    else (False,))
    return [(ov, flex, sd) for sd in contest_seeds(payload)
            for flex in machine_sets for ov in contenders]


def _run_jobs(payload: dict, pairs: list, *, processes=1, on_progress=None,
             should_cancel=None, poll_seconds=5.0):
    """Run a list of (overlap, flexible, seed) candidates. Returns (rows, done_evals,
    cancelled). processes>1 fans them across subprocesses (shared progress
    counter); processes<=1 runs them sequentially in-process."""
    rows, done_evals, cancelled = [], 0, False
    if processes <= 1:
        for ov, flex, sd in pairs:
            if should_cancel and should_cancel():
                cancelled = True
                break
            base = done_evals

            def cb(evals, best, _base=base):
                if on_progress:
                    on_progress(_base + evals, best)

            row = run_candidate(payload, ov, flex, seed=sd, on_progress=cb,
                                should_cancel=should_cancel)
            rows.append(row)
            done_evals += row.get("evals", 0)
            cancelled = cancelled or bool(row.get("cancelled"))
    else:
        import multiprocessing as mp
        ctx = mp.get_context()
        counter = ctx.Value("i", 0)
        stop = ctx.Value("b", 0)
        jobs = [(payload, ov, flex, sd) for ov, flex, sd in pairs]
        with ctx.Pool(processes=processes, initializer=_pool_init,
                      initargs=(counter, stop)) as pool:
            async_res = pool.map_async(_pool_run, jobs)
            while not async_res.ready():
                async_res.wait(poll_seconds)
                if on_progress:
                    on_progress(counter.value, None)
                if should_cancel and should_cancel():
                    stop.value = 1
            rows = async_res.get()
        done_evals = sum(r.get("evals", 0) for r in rows)
        cancelled = bool(stop.value) or any(r.get("cancelled") for r in rows)
    return rows, done_evals, cancelled


def merge_shard_rows(payload: dict, rows: list, evals: int, cancelled: bool) -> dict:
    """Reduce a set of run_candidate rows (any set of shards, or a whole
    contest) into the single result dict the app finalizes. pick_winner runs
    ONCE over the global row set. Same shape run_contest returns."""
    config = Config.from_dict(payload["config"])
    knob, _ = optimizer.knob_for(config)
    # None under "cp"; `getattr(config, None)` raises.
    cur_value = optimizer.knob_value(config)
    cur_flex = bool(getattr(config, "flexible_machines", False))
    winner = pick_winner(cur_value, cur_flex, rows, base_seed=int(payload["seed"]))
    # ``crew_rank`` and ``genome`` are kept in the STRIPPED table on purpose. The
    # non-sharded cloud worker posts this table back as its ``rows``
    # (scripts/cloud_optimize_worker.py, untouched here) and no top-level crew or
    # genome field survives that hop, so the table is the only place the app can
    # recover the winner's roster/decisions from — see ``crew_rank_of_winner`` and
    # ``genome_of_winner``. ``ranks`` stays stripped: it is posted separately and
    # is far larger.
    table = [{k: r[k] for k in ("overlap", "flexible", "seed", "eligible", "best",
                                "evals", "crew_rank", "genome")
              if k in r} for r in rows]
    if winner is None:
        return {"winner_overlap": cur_value, "winner_flexible": cur_flex, "rows": table,
                "knob": knob, "best": None, "ranks": {}, "winner_crew_rank": {},
                "winner_genome": {},
                "evals": evals, "cancelled": cancelled}
    return {"winner_overlap": winner["overlap"], "winner_flexible": bool(winner["flexible"]),
            "winner_crew_rank": dict(winner.get("crew_rank") or {}),
            # The CP decision genome the winning ranks were solved WITH. Applying
            # the ranks without it replays a job order with no machine, crew or
            # overlap decisions behind it: the decoder falls back per operation and
            # publishes a plan nobody searched. {} for every other engine.
            "winner_genome": dict(winner.get("genome") or {}),
            # Informational only: the seed is a SEARCH artifact, not a plan input.
            # The ranks are what gets applied, and they already encode the answer.
            "winner_seed": winner.get("seed"),
            "rows": table, "knob": knob, "best": winner["best"],
            "ranks": winner.get("ranks", {}), "evals": evals, "cancelled": cancelled}


def genome_of_winner(rows, overlap, flexible=False, best=None) -> dict:
    """The winning candidate's CP genome, recovered from a contest's rows.

    Exactly ``crew_rank_of_winner``'s problem and exactly its solution, for the
    other genome: the non-sharded cloud worker posts ``winner_overlap`` /
    ``ranks`` / ``best`` / ``rows`` and nothing else, so the decisions have to be
    read back out of the rows. Returns {} when nothing matches — and {} is safe,
    because ``_optimize_apply`` never overwrites a stored genome with an empty one.
    """
    hits = [r for r in (rows or [])
            if r.get("overlap") == overlap
            and bool(r.get("flexible")) == bool(flexible)
            and r.get("genome")]
    if best is not None and len(hits) > 1:
        exact = [r for r in hits if r.get("best") == best]
        if exact:
            hits = exact
    return dict(hits[0]["genome"]) if hits else {}


def crew_rank_of_winner(rows, overlap, flexible=False, best=None) -> dict:
    """The winning candidate's crew genome, recovered from a contest's rows.

    Why this exists: the non-sharded cloud worker posts ``winner_overlap`` /
    ``ranks`` / ``best`` / ``rows`` and nothing else (scripts/ is untouchable, and
    an old worker on a running job could not be updated anyway), so the crew genome
    has to be read back out of the rows it already carries. Matching is
    (overlap, flexible) first and the winning ``best`` second, because a
    multi-seed contest can run the same overlap several times. Returns {} when
    nothing matches — a plan with no crew genome simply replays the saved one.
    """
    hits = [r for r in (rows or [])
            if r.get("overlap") == overlap and bool(r.get("flexible")) == bool(flexible)]
    if best is not None and len(hits) > 1:
        exact = [r for r in hits if r.get("best") == best]
        if exact:
            hits = exact
    for r in hits:
        if r.get("crew_rank"):
            return dict(r["crew_rank"])
    return {}


def run_contest(payload: dict, *, processes=1, on_progress=None,
                should_cancel=None, poll_seconds=5.0) -> dict:
    """The full fair contest from a payload. ``processes > 1`` fans the
    contenders out to subprocesses (per-eval progress via a shared counter);
    ``processes == 1`` runs them sequentially in-process. Returns
    {winner_overlap, rows, best, ranks, evals, cancelled}."""
    pairs = contest_jobs(payload)
    rows, done_evals, cancelled = _run_jobs(
        payload, pairs, processes=processes, on_progress=on_progress,
        should_cancel=should_cancel, poll_seconds=poll_seconds)
    if on_progress:
        on_progress(done_evals, None)
    return merge_shard_rows(payload, rows, done_evals, cancelled)


def run_contest_slice(payload: dict, shard_index: int, shard_total: int, *,
                      processes=1, on_progress=None, should_cancel=None,
                      poll_seconds=5.0) -> dict:
    """One shard of the contest: run the round-robin slice
    contest_jobs(payload)[shard_index::shard_total] and return its RAW rows
    (with ranks) for the app to merge. shard_total<=1 runs every candidate."""
    pairs = contest_jobs(payload)
    if shard_total and shard_total > 1:
        pairs = pairs[shard_index::shard_total]
    rows, done_evals, cancelled = _run_jobs(
        payload, pairs, processes=processes, on_progress=on_progress,
        should_cancel=should_cancel, poll_seconds=poll_seconds)
    return {"rows": rows, "evals": done_evals, "cancelled": cancelled}
