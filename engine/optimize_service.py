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


def cloud_candidates(config) -> tuple:
    """The cloud contest lineup for this config's scheduler mode."""
    sched = getattr(config, "scheduler", "classic")
    if sched == "flow":
        return CLOUD_FLOW_CHUNK_CANDIDATES
    if sched == "new":
        return CLOUD_NEW_OVERLAP_CANDIDATES
    return CLOUD_OVERLAP_CANDIDATES


CLOUD_BUDGET_PER_CANDIDATE = 400
# The new engine's decode is heavier, so its fine grid gets a smaller per-candidate budget
# (12 candidates × 150 ≈ 1,800 plans, ~15 min across 2 cores).
CLOUD_NEW_BUDGET_PER_CANDIDATE = 150


def cloud_budget(config) -> int:
    """Plans per candidate for the cloud contest, per scheduler mode.

    OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE (env, positive int) overrides both modes —
    the deep-compute knob (2026-08-01 Oracle-worker spec: 300 ≈ the measured −4.4%
    class on a 4-core box). Unset/invalid/≤0 → the mode defaults below.
    """
    import os
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
                  absences=None, operator_table=None, frozen=None) -> dict:
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
def pick_winner(current_overlap, current_flexible, rows):
    """Best score wins; an exact tie keeps the current (overlap, machine-set)."""
    def _is_current(r):
        return r.get("overlap") == current_overlap and bool(r.get("flexible")) == bool(current_flexible)
    ordered = sorted(rows, key=lambda r: (not _is_current(r), r.get("overlap")))
    best = None
    for r in ordered:
        if not r.get("eligible") or r.get("best") is None:
            continue
        if best is None or optimizer.score(r["best"]) < optimizer.score(best["best"]):
            best = r
    return best


def run_candidate(payload: dict, overlap: int, flexible: bool = False, *, on_progress=None,
                  should_cancel=None) -> dict:
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
    setup = prepare_contest(orders, actuals, masters, config, absences=absences,
                            operator_table=operator_table, frozen=frozen)
    knob, _cands = optimizer.knob_for(setup.search_config)
    cfg = replace(setup.search_config, flexible_machines=bool(flexible), **{knob: int(overlap)})
    res = optimizer.optimize(setup.target, cfg, setup.masters,
                             reserved=setup.absence_reserved,
                             frozen=setup.frozen,
                             budget_evals=int(payload["budget_per_candidate"]),
                             seed=int(payload["seed"]),
                             on_progress=on_progress, should_cancel=should_cancel)
    return {"overlap": int(overlap), "flexible": bool(flexible), "eligible": True,
            "best": res.best, "evals": res.evals, "ranks": res.ranks, "cancelled": res.cancelled}


# Subprocess plumbing: a plain module-level initializer + runner so the pool
# works under both fork (Linux runner) and spawn (macOS dev) start methods.
_POOL = {"counter": None, "stop": None}


def _pool_init(counter, stop):
    _POOL["counter"], _POOL["stop"] = counter, stop


def _pool_run(args):
    payload, overlap, flexible = args
    last = {"evals": 0}

    def cb(evals, _best):
        delta, last["evals"] = evals - last["evals"], evals
        c = _POOL["counter"]
        if c is not None:
            with c.get_lock():
                c.value += delta

    stop = _POOL["stop"]
    return run_candidate(payload, overlap, flexible, on_progress=cb,
                         should_cancel=(lambda: bool(stop.value)) if stop else None)


def contest_jobs(payload: dict) -> list:
    """The ordered (overlap, flexible) candidate list a contest evaluates — the
    SINGLE source of truth for run_contest AND the sharded worker, so they can
    never drift. Order: machine-set outer, overlap inner (matches run_contest)."""
    config = Config.from_dict(payload["config"])
    knob, _ = optimizer.knob_for(config)
    cur_value = getattr(config, knob)
    contenders = optimizer.sweep_contenders(cur_value, payload["candidates"])
    # The machine-set dimension only affects the new engine (flexible_machines is
    # inert for classic/flow — see engine/new_engine._new_masters). Gate the outer
    # loop on scheduler so classic/flow cloud contests stay single-pass and byte-
    # identical to their local counterpart (Task 3 established this same gate in
    # engine/optimizer.sweep_optimize / engine/new_engine.sweep_optimize).
    machine_sets = (False, True) if getattr(config, "scheduler", "classic") == "new" else (False,)
    return [(ov, flex) for flex in machine_sets for ov in contenders]


def _run_jobs(payload: dict, pairs: list, *, processes=1, on_progress=None,
             should_cancel=None, poll_seconds=5.0):
    """Run a list of (overlap, flexible) candidates. Returns (rows, done_evals,
    cancelled). processes>1 fans them across subprocesses (shared progress
    counter); processes<=1 runs them sequentially in-process."""
    rows, done_evals, cancelled = [], 0, False
    if processes <= 1:
        for ov, flex in pairs:
            if should_cancel and should_cancel():
                cancelled = True
                break
            base = done_evals

            def cb(evals, best, _base=base):
                if on_progress:
                    on_progress(_base + evals, best)

            row = run_candidate(payload, ov, flex, on_progress=cb, should_cancel=should_cancel)
            rows.append(row)
            done_evals += row.get("evals", 0)
            cancelled = cancelled or bool(row.get("cancelled"))
    else:
        import multiprocessing as mp
        ctx = mp.get_context()
        counter = ctx.Value("i", 0)
        stop = ctx.Value("b", 0)
        jobs = [(payload, ov, flex) for ov, flex in pairs]
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
    cur_value = getattr(config, knob)
    cur_flex = bool(getattr(config, "flexible_machines", False))
    winner = pick_winner(cur_value, cur_flex, rows)
    table = [{k: r[k] for k in ("overlap", "flexible", "eligible", "best", "evals")
              if k in r} for r in rows]
    if winner is None:
        return {"winner_overlap": cur_value, "winner_flexible": cur_flex, "rows": table,
                "knob": knob, "best": None, "ranks": {}, "evals": evals,
                "cancelled": cancelled}
    return {"winner_overlap": winner["overlap"], "winner_flexible": bool(winner["flexible"]),
            "rows": table, "knob": knob, "best": winner["best"],
            "ranks": winner.get("ranks", {}), "evals": evals, "cancelled": cancelled}


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
