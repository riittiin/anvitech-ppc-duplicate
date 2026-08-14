"""The decision genome — what the solver decided, in a shape a page-load-speed
replay can consume.

The solve runs off-box and takes minutes; ``/run`` has to answer in seconds. So
the solve does not run again on every page load — it runs ONCE, and this module
carries its answer (job order, machine per op, crew roster, released pieces,
solved completion dates, the book it was solved against) to a later task's
decoder, which replays it against today's book in milliseconds.

REPLAY PATH — imports pyjobshop/ortools NEITHER directly nor transitively. This
module runs on the production server, which deliberately has neither installed
(see ``cp_engine/__init__.py``). It is pure dict manipulation: read a solved
model's decisions into plain Python, and round-trip those decisions through
JSON. Nothing here calls a solver.

Why the JSON round-trip needs its own care: four of the eight keys are
naturally keyed by TUPLES — ``(machine, shift_index)`` for the roster and
``(job_key, op_seq)`` for the machine assignment, the solved start and the
bench operator — and JSON has no tuple-keyed object. The
tempting shortcut is ``str(key)``, and it is a silent defect: ``from_json``
would produce a dict keyed by the STRING ``"('CNC1', 0)"``, every replay lookup
against the real tuple ``('CNC1', 0)`` would miss, and the decoder would fall
back to "unassigned" for every single operation — while the genome still looks
perfectly well-formed (right key count, right value types, no exception
anywhere). So both directions go through ``KEY_SEP`` instead: the SAME
separator ``book_store`` already uses to flatten a persisted ``"<so>\\x1f<item>"``
rank map to a JSON object, imported from ``engine.pipeline`` rather than
restated, so a genome round-trips through the existing store unchanged.

The two tuple-keyed maps are also asymmetric in what their SECOND component is:
``cp_roster``'s is a shift INDEX and ``cp_machine_of``'s is an op SEQ — both
ints. ``str(int)`` survives a JSON round-trip as a string, not an int, so
``from_json`` explicitly casts each back with ``int(...)`` rather than trusting
whatever ``str.split`` handed back. A round-trip test that only checks the
looked-up VALUE (e.g. the operator name) would not catch a shift index quietly
turning into ``"0"`` — the value at the wrong-typed key would still read right
until the day two entries collide because ``0`` and ``"0"`` hash differently
from an int the decoder actually holds; the test in this module's test file
compares the whole dict, keys included, for exactly that reason.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from engine.pipeline import KEY_SEP

# The eight things the solver decided (see the design spec, "The genome, and the
# fast replay"). Order is the one every ``to_json``/``from_json`` round trip and
# every "does this genome have everything" check iterates in — fixed here so it
# is never re-typed, and re-typed differently, at a second call site.
KEYS = (
    "ranks",
    "cp_machine_of",
    "cp_roster",
    "cp_overlap_of",
    "cp_completion",
    "cp_solved_book_sig",
    "cp_start_of",
    "cp_bench_of",
)

# Genome keys whose value is a dict keyed by a 2-tuple, and so need the
# KEY_SEP encoding. The other four (``ranks``, ``cp_overlap_of``,
# ``cp_completion``, ``cp_solved_book_sig``) are already JSON-native (string or
# scalar keys/values) and pass through untouched. Every VALUE is JSON-native in
# all eight — ``cp_start_of`` carries an ISO datetime STRING, not a datetime,
# for exactly that reason.
_TUPLE_KEYED = ("cp_machine_of", "cp_roster", "cp_start_of", "cp_bench_of")


def to_json(g: dict) -> dict:
    """A genome dict -> a plain JSON-safe dict.

    Only the documented ``KEYS`` are considered — ``from_solution`` never
    produces anything else, so there is nothing to preserve beyond them; an
    input dict with no ``KEYS`` present (``{}``) produces ``{}``, not a padded
    eight-key skeleton, so an empty genome round-trips to an empty genome.
    """
    out: dict = {}
    for key in KEYS:
        if key not in g:
            continue
        value = g[key]
        if key in _TUPLE_KEYED:
            out[key] = {
                f"{parts[0]}{KEY_SEP}{parts[1]}": v
                for parts, v in value.items()
            }
        else:
            out[key] = value
    return out


def is_flat(g: dict | None) -> bool:
    """Has this genome already been through ``to_json``?

    ONE definition of the shape question, because ``to_json`` is **not
    idempotent** and the wrong answer is silent. Handed an already-flat genome,
    ``to_json`` rebuilds each key from the first two CHARACTERS of the string
    (``"CNC1\\x1f1"`` -> ``("C", "N")``): no exception, no malformed genome — every
    later lookup simply MISSES, the decoder falls back for every single
    operation, and the plan looks perfectly well-formed.

    A genome arrives flat whenever it came home over the wire (a cloud worker's
    row, the store's JSON), and tuple-keyed straight out of ``from_solution``.
    Both shapes reach the same call sites, so every one of them asks here.
    """
    return any(isinstance(key, str)
               for name in _TUPLE_KEYED
               for key in ((g or {}).get(name) or {}))


def as_json(g: dict | None) -> dict:
    """``to_json``, made safe to call on a genome of EITHER shape.

    Use this at any boundary that must hand JSON onward but cannot prove which
    shape it was given — the cloud contest row (``optimize_service.run_candidate``)
    and the store write (``book_store.save_cp_genome``). An already-flat genome
    passes through untouched; a tuple-keyed one is flattened exactly once.
    """
    if not g:
        return {}
    return dict(g) if is_flat(g) else to_json(g)


def from_json(raw: dict) -> dict:
    """The inverse of ``to_json``.

    Drops any key outside ``KEYS`` (forward compatibility in the safe
    direction: a genome written by a NEWER version of this module must not
    smuggle a key this version does not know how to honour into a replay), and
    restores both tuple-keyed maps with their second component cast back to
    ``int`` — ``cp_roster``'s shift index and ``cp_machine_of``'s op seq are
    both ints on the way in, and a bare ``str.split`` would hand back strings.
    """
    out: dict = {}
    for key in KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in _TUPLE_KEYED:
            out[key] = {
                _tuple_key(flat): v for flat, v in value.items()
            }
        else:
            out[key] = value
    return out


def _tuple_key(flat: str) -> tuple:
    """``"machine\\x1f0"`` -> ``("machine", 0)``. Splits from the RIGHT, once —
    the first component (a machine id or a batch key) is free to contain
    anything, including a stray ``KEY_SEP``; the second component never does,
    because it is always an int rendered by ``str()``."""
    head, _, tail = flat.rpartition(KEY_SEP)
    return (head, int(tail))


def from_solution(result, built, roster, released, plan_start) -> dict:
    """Read what the solver decided off a solved model into a genome dict.

    ``result`` is a ``pyjobshop.Result``: ``result.best.tasks[task_idx]`` and
    ``result.best.jobs[job_idx]`` are pyjobshop's own ``ScheduledTask``/
    ``ScheduledJob`` — plain ints/lists already resolved by pyjobshop itself,
    so reading them needs no solver object and no import here.

    ``roster`` (``cp_engine.rules.Roster``) and ``released`` (``add_release``'s
    ``{job_key: k}``) are NOT pyjobshop objects — ``cp_engine.rules`` builds
    them directly on the raw ortools ``CpModel``, and a raw ``BoolVar``/
    ``IntVar`` only becomes a plain value through ``CpSolver.Value(...)``. This
    module must never import ortools (it is on the Render replay path), so
    that resolution has to happen in the solve step (worker-only) BEFORE this
    function is called: every value in ``roster.x`` and every value in
    ``released`` must already be a plain ``int``/``bool`` by the time it gets
    here. This is checked, not assumed — ``_int_value`` raises rather than
    accepting a raw CP variable, because a ``BoolVar``/``IntVar`` is always
    object-truthy: silently accepting one would not raise, would not look
    wrong, and would just roster every physically-possible pairing as staffed.

    NOTE (2026-08-14): the objects this reads — the solve step, and the exact
    shape of ``result``/``roster``/``released`` at the call site — are built by
    a task that does not exist yet. This function is written to the shapes the
    brief and ``cp_engine.model``/``cp_engine.rules`` already describe, kept
    small, and NOT exercised end-to-end. The task that builds the solve owns
    wiring it up and should adjust this if a shape differs.
    """
    machine_by_res = {idx: mid for mid, idx in built.machine_res_order.items()}

    ranks = {}
    order = sorted(built.jobs, key=lambda job: (_job_start(built, result, job), job.key))
    for i, job in enumerate(order):
        for so in job.so_refs:
            ranks[f"{so}{KEY_SEP}{job.item_code}"] = i + 1

    operator_by_res = {idx: name
                       for name, idx in built.operator_res_order.items()}

    cp_machine_of, cp_start_of, cp_bench_of = {}, {}, {}
    for (job_key, op_seq), task_idx in built.task_of.items():
        scheduled = result.best.tasks[task_idx]
        for res_idx in scheduled.resources:
            mid = machine_by_res.get(res_idx)
            if mid is not None:
                cp_machine_of[(job_key, op_seq)] = mid
                break
        # ABSOLUTE, not minutes-from-plan-start: a floor is only meaningful
        # against a fixed wall clock, so an absolute one is self-describing and a
        # replay on a DIFFERENT plan clock gets floors that are visibly inert
        # rather than floors that silently re-anchor. It is NOT a defence against
        # the plan clock moving — the shift indices in ``cp_roster`` and the
        # decoder's whole calendar are counted from plan_start, so a genome under
        # a moved clock is stale whatever this key holds. See
        # ``decode._floors``.
        cp_start_of[(job_key, op_seq)] = (
            plan_start + timedelta(minutes=scheduled.start)).isoformat()
        # WHO the solver put at a bench. Rule 1 rosters CNC/VMC only, so
        # ``roster.x`` — and therefore ``cp_roster`` — names nobody here; but
        # ``model._add_modes`` really does book a capacity-1 operator renewable
        # on every manual/inspection mode, and throwing that away meant the
        # decoder re-invented it and the two disagreed about who was free when.
        # A machining mode carries no operator at all, so this map is naturally
        # bench-only and never fights the roster.
        for res_idx in scheduled.resources:
            name = operator_by_res.get(res_idx)
            if name is not None:
                cp_bench_of[(job_key, op_seq)] = name
                break

    cp_roster = {}
    for (operator, machine, shift_idx), value in sorted(roster.x.items()):
        key = (machine, shift_idx)
        if key in cp_roster:
            continue                      # first (sorted) staffed operator wins
        if _int_value(value, f"roster.x[{(operator, machine, shift_idx)!r}]"):
            cp_roster[key] = operator

    cp_overlap_of = {
        job_key: _int_value(k, f"released[{job_key!r}]")
        for job_key, k in released.items()
    }

    cp_completion = {}
    for job_key, job_idx in built.job_of.items():
        scheduled_job = result.best.jobs[job_idx]
        end_dt = plan_start + timedelta(minutes=scheduled_job.end)
        cp_completion[job_key] = end_dt.date().isoformat()

    return {
        "ranks": ranks,
        "cp_machine_of": cp_machine_of,
        "cp_roster": cp_roster,
        "cp_overlap_of": cp_overlap_of,
        "cp_completion": cp_completion,
        "cp_solved_book_sig": _book_signature(built.jobs),
        "cp_start_of": cp_start_of,
        "cp_bench_of": cp_bench_of,
    }


def _job_start(built, result, job) -> int:
    """The earliest solved start among a job's in-house/outsourced tasks. A job
    with no task at all (nothing but a DISPATCH milestone, or every step
    already fully produced) sorts first — there is nothing to wait on."""
    idxs = [built.task_of[(job.key, op.seq)] for op in job.ops
            if (job.key, op.seq) in built.task_of]
    if not idxs:
        return 0
    return min(result.best.tasks[i].start for i in idxs)


def _int_value(value, what: str) -> int:
    """Reject anything that is not already a resolved plain value.

    A raw ortools ``BoolVar``/``IntVar`` is an object, and every object is
    truthy — ``bool(some_bool_var)`` is always ``True`` whether the solver set
    it or not. Accepting one here without checking would not crash; it would
    just silently roster every physically-possible pairing as staffed and
    release every job on its maximum k. Better to raise loud, here, once."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    raise TypeError(
        f"{what} is a {type(value).__name__}, not an int/bool — "
        "genome.from_solution never touches ortools, so the caller must "
        "resolve every roster/release variable (CpSolver.Value(...)) before "
        "calling this function."
    )


def _book_signature(jobs) -> str:
    """Fingerprint of the book the solve saw, straight off ``built.jobs`` — that
    IS the book model.build() was given, so there is no need for a caller to
    pass one in separately. A later task's drift check compares this against a
    freshly-computed signature of TODAY's book to decide whether the genome is
    stale.

    Deliberately its own (tiny) computation rather than a reuse of
    ``engine.optimize_service.book_signature`` — that function reads
    ``engine.models.SOLine`` fields (``process_qty``, ``commitment``, ...);
    this reads ``cp_engine.domain.Job``/``Op`` fields, a different shape
    entirely, and duck-typed rather than imported so this module never has to
    import ``cp_engine.domain`` (which itself imports ``engine.loaders``) at
    all."""
    rows = sorted(
        (
            job.key,
            job.item_code,
            int(job.qty),
            job.due.isoformat() if job.due else None,
            sorted(job.so_refs),
            sorted((job.remaining or {}).items()),
            tuple(
                (op.seq, op.name, op.kind, op.cycle_min, tuple(op.machine_options))
                for op in job.ops
            ),
        )
        for job in jobs
    )
    blob = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
