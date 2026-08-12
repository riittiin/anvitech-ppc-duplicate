"""The seam between the app and ``roster_engine``.

The ONLY file that knows both worlds. Everything upstream (loader, order book,
store, freeze, Rules 1-3) and everything downstream (Gantt, Schedule tab, delay
report, Analytics, shift-wise export, efficiency report, Daily Entry) is
untouched, because this returns exactly the ``ScheduleEntry`` list they already
consume. That is what makes an A/B against the live engine exact.

Contract notes that are load-bearing elsewhere:

  * ``op_segments`` is a **list** of ``(start, end, operator)`` tuples sorted by
    start. Five surfaces read it, and ``rule6_allocate``'s operator-balance pass
    assigns into it by index — a tuple would raise there.
  * OS and off-machine entries carry the lane STRINGS below as their machine.
    ``delay_report._OFF_LANES``, ``analytics.NON_MACHINE_LANES`` and
    ``freeze._OS_LANES`` all match on them literally; anything else bills
    outsourcing to an in-house machine (the 2026-08-09 defect).
  * every real machine entry that occupies time names an operator, because
    ``freeze.py`` pins machine AND operator, so an empty name freezes a ghost.

THE FROZEN-ROW TRANSLATION is the delicate part and it lives in ``_pins``. The
producer is ``engine.freeze.compute_frozen_set``, whose rows are
``{so_no, item_code, process, op_seq, machine, operator, remaining_qty,
prev_start}`` — no ``order_key``, no ``machine_id``, keyed per **SO LINE** — while
a ``roster_engine`` job key is a **BATCH** id. Handed over raw, every row is
dropped and the plan looks perfectly well-formed while the in-progress part is
planned on a machine it is not physically on, paying a 90-minute setup it does
not owe. See ``_pins`` for the five things that translation has to get right.
"""

from __future__ import annotations

from datetime import datetime

from engine.loaders import normalize_process_name, normalize_resource_id
from engine.models import ScheduleEntry
from roster_engine import SCHEDULER_FINGERPRINT  # noqa: F401  (re-exported)
from roster_engine import scheduler
from roster_engine.domain import OUTSOURCED, build_jobs, build_shop

# The exact strings every off-machine consumer matches on. Pinned by test against
# delay_report._OFF_LANES / analytics.NON_MACHINE_LANES / freeze._OS_LANES.
OS_LANE = "OS / Outsourced"
OFF_LANE = "Off-machine"


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, frozen=None, **kw):
    """Scheduler seam contract: prioritized ``batches`` -> list[ScheduleEntry].

    The batches arrive already in priority order (Rules 1-3, or a saved
    optimization's rank map), so that order IS the job sequence. Never
    re-consolidated here — Rule 1 already clubbed the SO lines.

    ``machine_lost_min`` is accepted for signature compatibility and ignored, as
    it is by the ``new`` and ``flow`` engines; no caller in the app passes one.
    """
    say = notes if notes is not None else []
    if not batches or masters is None:
        return []

    jobs, batch_by_key, skipped = build_jobs(batches, masters)
    if skipped:
        say.append("skipped %d item(s) with no routing: %s"
                   % (len(skipped), ", ".join(sorted(skipped)[:5])))
    if not jobs:
        return []

    shop = build_shop(masters, _absent_from_reserved(reserved, masters, say))
    pins = _pins(frozen, batch_by_key, masters, say)
    plan = scheduler.schedule(
        jobs, [j.key for j in jobs], shop, config,
        overlap=_overlap(config),
        crew_rank=dict(getattr(config, "crew_rank", None) or {}),
        frozen=pins)
    _check_pins_landed(pins, plan, say)
    return _entries(plan, batch_by_key)


# --------------------------------------------------------------------------- #
# Config translation
# --------------------------------------------------------------------------- #

def _overlap(config) -> float:
    """``overlap_percent`` 0-100 -> the fraction of pieces that must clear a step
    before its successor may start. 80 means "80 of 100 pieces are done", which is
    what RULES.md, Config's own docstring and ``roster_engine.release`` all say.

    ``overlap_mode`` is deliberately IGNORED, exactly as ``new_engine._plan_config``
    ignores it: the overlap is optimizer-owned and persisted as a percentage, so
    honouring the legacy mode switch would silently discard a tuned value.
    """
    raw = float(getattr(config, "overlap_percent", 100) or 0.0)
    return min(1.0, max(0.0, raw / 100.0))


def _absent_from_reserved(reserved, masters, say) -> dict:
    """``reserved`` maps machine id / operator name -> busy ``(start, end)``
    intervals. Only the operator entries mean anything to this engine: an absent
    person cannot be rostered, and ``Shop`` has no machine-reservation concept.

    Today's only live producer is ``optimize_service.absence_reservations``, which
    emits operator names only. A key that is NOT an operator is therefore
    unexpected — it is reported rather than swallowed, because a constraint that
    quietly does nothing is this codebase's most expensive recurring defect.
    """
    if not reserved:
        return {}
    names = {o.name for o in masters.operators}
    out = {k: list(v) for k, v in reserved.items() if k in names}
    ignored = sorted(k for k in reserved if k not in names)
    if ignored:
        say.append("ignored %d reserved block(s) that name no operator in "
                   "Settings (the roster engine reserves people, not machines): %s"
                   % (len(ignored), ", ".join(ignored[:5])))
    return out


# --------------------------------------------------------------------------- #
# Frozen rows: SO LINES in, BATCH operations out
# --------------------------------------------------------------------------- #

def _value(row, *names):
    """One value out of a frozen row, whatever it is spelled. Blank counts as
    absent, so an empty ``machine`` falls through to the next spelling rather than
    pinning to ""."""
    for name in names:
        value = (row.get(name) if hasattr(row, "get")
                 else getattr(row, name, None))
        if value is not None and value != "":
            return value
    return None


def _prev_start_key(row):
    """A sortable, type-stable previous-plan start. ``compute_frozen_set`` emits an
    ISO **string**; other callers hand a ``datetime``. Both normalise to an ISO
    string, which sorts chronologically; a missing one sorts last."""
    value = _value(row, "prev_start", "start")
    if isinstance(value, datetime):
        return (0, value.isoformat())
    text = str(value).strip() if value is not None else ""
    return (0, text) if text else (1, "")


def _flatten(frozen) -> list:
    """``book_store.load_frozen_ops`` returns a flat list; ``run_forward``'s
    docstring types ``frozen`` as {machine id -> rows}. Accept both."""
    if not frozen:
        return []
    if isinstance(frozen, dict):
        return [row for _mid, rows in sorted(frozen.items())
                for row in (rows or ())]
    return list(frozen)


def _so_refs(batch) -> list:
    refs = getattr(batch, "source_so_refs", None)
    if refs is None:
        refs = getattr(batch, "so_refs", None)
    return list(refs or ())


def _describe(row) -> str:
    who = _value(row, "so_no") or _value(row, "order_key") or "?"
    item = _value(row, "item_code") or ""
    seq = _value(row, "op_seq", "process_seq")
    mid = _value(row, "machine_id", "machine") or "?"
    return "%s%s step %s on %s" % (who, "/" + str(item) if item else "", seq, mid)


def _seq_for(row, batch, masters):
    """The routing step this row pins. Trust ``op_seq``; fall back to matching the
    routing by normalised process name, the way ``new_engine._ppc_frozen`` does."""
    raw = _value(row, "op_seq", "process_seq")
    if raw is not None:
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, "step %r is not a number" % (raw,)
    want = normalize_process_name(_value(row, "process", "process_name") or "")
    routing = masters.routings.get(batch.item_code) if want else None
    if routing is not None:
        for proc in routing.processes:
            if normalize_process_name(proc.name) == want:
                return int(proc.seq), None
    return None, "the row names no routing step"


def _pins(frozen, batch_by_key, masters, say) -> list:
    """Frozen rows -> the pin list ``roster_engine.scheduler.schedule`` understands.

    Five things this has to get right, each of them a live defect if it does not:

    1. **Key translation.** The rows are keyed by ``(so_no, item_code)`` — an SO
       LINE — while a job key is a BATCH id, because Rule 1 clubs several lines
       into one batch. Mapped through ``Batch.source_so_refs``. (An explicit
       ``order_key``/``job_key`` is honoured first for callers that have one.
       ``batch_id`` deliberately is NOT: Rule 1 mints positional ids, ``B001``,
       ``B002``, … afresh on every plan, so a previous plan's batch id can name a
       completely different batch.)
    2. **One pin per (batch, op).** Several clubbed lines can each be in progress
       on the same step, and an operation runs once, on one machine. The surviving
       row is the earliest-started one, with machine/operator/SO breaking ties, so
       the answer cannot depend on the order rows arrive in.
    3. **Machine-id normalisation.** ``op.machine_options`` are normalised by
       ``loaders.parse_resource_candidates``; an applied plan may spell a machine
       the master's way ("CNC 4"), and an un-normalised pin is compared as a raw
       string and silently dropped.
    4. **Both spellings and both time types.** ``machine`` / ``machine_id``,
       ``prev_start`` as a datetime or an ISO string.
    5. **Never the quantity.** ``remaining_qty`` is ONE SO line's remainder while
       the operation it pins belongs to the whole batch; it is dropped from the
       pin entirely so nothing downstream can read it. Taking it dropped 281
       pieces of a clubbed order into no plan at all (live 2026-08-11). The
       quantity comes from the batch, via ``Job.qty_for``.

    Rows that cannot be translated are reported through ``say`` — never dropped in
    silence, and never raised: one stale row must not cost the whole book its plan.
    """
    rows = _flatten(frozen)
    if not rows:
        return []

    so_to_key = {}
    for key, batch in batch_by_key.items():
        for so in _so_refs(batch):
            so_to_key.setdefault((str(so), batch.item_code), key)

    best: dict = {}
    unmapped: list = []
    superseded = 0
    for row in rows:
        key = _value(row, "order_key", "job_key")
        key = str(key) if key is not None and str(key) in batch_by_key else None
        if key is None:
            key = so_to_key.get((str(_value(row, "so_no") or ""),
                                 str(_value(row, "item_code") or "")))
        if key is None:
            unmapped.append((row, "no batch in this plan covers it"))
            continue
        seq, why = _seq_for(row, batch_by_key[key], masters)
        if seq is None:
            unmapped.append((row, why))
            continue
        mid = normalize_resource_id(_value(row, "machine_id", "machine") or "")
        if not mid:
            unmapped.append((row, "the row names no machine"))
            continue
        rank = (_prev_start_key(row), mid,
                str(_value(row, "operator") or ""),
                str(_value(row, "so_no") or ""))
        pin = dict(row) if isinstance(row, dict) else {
            k: getattr(row, k) for k in ("so_no", "item_code", "process",
                                         "op_seq", "machine", "operator",
                                         "prev_start") if hasattr(row, k)}
        pin["order_key"] = key
        pin["op_seq"] = seq
        pin["machine_id"] = mid
        # A pin says WHERE and WHEN, never HOW MUCH — so the number cannot even be
        # read by accident further down.
        pin.pop("remaining_qty", None)
        current = best.get((key, seq))
        if current is None or rank < current[0]:
            superseded += 1 if current is not None else 0
            best[(key, seq)] = (rank, pin)
        else:
            superseded += 1

    if unmapped:
        say.append("frozen work: %d in-progress row(s) could not be matched to "
                   "this plan and were left to normal scheduling — %s"
                   % (len(unmapped),
                      "; ".join("%s (%s)" % (_describe(r), why)
                                for r, why in unmapped[:5])))
    if superseded:
        say.append("frozen work: %d row(s) describe an operation another row "
                   "already covers (clubbed SO lines on one batch step); one "
                   "machine each, as an operation runs once." % superseded)
    return [pin for _key, (_rank, pin) in sorted(best.items())]


def _check_pins_landed(pins, plan, say):
    """The accounting must close: every pin handed over is either applied or named
    in ``Plan.unpinned``. A pin the engine could not honour is a real, reportable
    fact — the part is planned on a machine it is not on and pays a setup it does
    not owe — so it is surfaced rather than trusted to be noticed."""
    unpinned = list(getattr(plan, "unpinned", ()) or ())
    applied = len(pins) - len(unpinned)
    if applied == len(pins):
        return
    say.append("frozen work: %d of %d in-progress operation(s) could not be held "
               "on the machine they are running on — %s"
               % (len(unpinned), len(pins),
                  "; ".join("%s (%s)" % (_describe(row), why)
                            for row, why in unpinned[:5])))


# --------------------------------------------------------------------------- #
# Placements -> ScheduleEntry
# --------------------------------------------------------------------------- #

def _entries(plan, batch_by_key) -> list:
    out = []
    for p in plan.placements:
        batch = batch_by_key.get(p.job_key)
        if batch is None:                       # cannot happen; never guess a batch
            continue
        if p.machine is None:
            # An outsourced block goes to the OS lane; every other machine-less
            # step (DISPATCH, and a step a re-plan has nothing left to make on) is
            # an off-machine milestone. Both lanes are matched literally by the
            # delay report, Analytics and freeze.
            machine = OS_LANE if p.kind == OUTSOURCED else OFF_LANE
            segments, operator = [], ""
        else:
            machine = p.machine
            segments = [(s, e, who) for s, e, who in p.segments]
            operator = segments[0][2] if segments else ""
        out.append(ScheduleEntry(
            batch_id=str(batch.batch_id),
            item_code=batch.item_code,
            process_seq=p.op_seq,
            process_name=p.op_name,
            machine=machine,
            qty=p.qty,
            occupancy_min=p.work_min,
            start=p.start,
            end=p.end,
            so_refs=_so_refs(batch),
            operator=operator,
            op_segments=segments))
    out.sort(key=lambda e: (e.start, e.batch_id, e.process_seq))
    return out
