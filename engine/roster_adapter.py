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
  * EVERY real-machine entry names an operator and carries at least one segment —
    not merely the ones that occupy time. ``freeze.py`` pins machine AND operator,
    so an empty name freezes a ghost, and a zero-work step (a real bench with a
    blank cycle time) is not invisible: overlap opens it early and pacing holds
    its end to its predecessor's, so it is drawn, billed and frozen like any other.

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

import dataclasses
from datetime import date

from engine.loaders import normalize_process_name, normalize_resource_id
from engine.models import ScheduleEntry
from engine.pipeline import RuleError
from roster_engine import SCHEDULER_FINGERPRINT  # noqa: F401  (re-exported)
from roster_engine import scheduler
# ONE definition of how a frozen row is read and which of two rows for one
# operation wins — see ``roster_engine.scheduler``'s "authoritative definitions"
# block. This file used to keep its own copies with a DIFFERENT rank tuple.
from roster_engine.scheduler import pin_rank, prev_start_key, row_value
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
    try:
        plan = scheduler.schedule(
            jobs, [j.key for j in jobs], shop, _resolved(config),
            overlap=_overlap(config),
            crew_rank=dict(getattr(config, "crew_rank", None) or {}),
            frozen=pins)
    except (scheduler.Unschedulable, scheduler.PlanStartMissing) as err:
        raise _rule_error(err) from err
    _check_pins_landed(pins, plan, say)
    _report_dropped(plan, batch_by_key, say)
    return _entries(plan, batch_by_key)


# --------------------------------------------------------------------------- #
# The Optimize feature's entry points
# --------------------------------------------------------------------------- #

def optimize_sequence(so_lines, config, masters, *, reserved=None, budget_evals=150,
                      seed=42, on_progress=None, should_cancel=None, frozen=None):
    """Sequence + CREW search at this config's overlap, as an ``OptimizeResult``.

    The cloud contest sweeps the overlap EXTERNALLY (one Actions shard per
    candidate) and calls this once per candidate, so across candidates it becomes
    the full overlap x sequence x crew contest — distributed. The app's own result
    type is returned so the contest, the panel and the apply path are unchanged;
    the winning crew genome rides on ``OptimizeResult.crew_rank``.

    ``frozen`` is not optional decoration here. Unlike the retired classic/flow
    engines, ``run()`` HONOURS it (it pins in-progress work to the machine it is
    physically on), so a search that ignored it would score every candidate on a
    plan the app will never build, and the ranks it picked would be answers to the
    wrong question.
    """
    from engine.optimizer import OptimizeResult, plan_metrics, ranks_for
    from engine.rules import rule1_consolidate
    from roster_engine import search as roster_search

    config.validate()
    batches = rule1_consolidate.run(list(so_lines), config=config, masters=masters)
    if not batches:
        return OptimizeResult()
    say: list = []
    jobs, batch_by_key, _skipped = build_jobs(batches, masters)
    if not jobs:
        return OptimizeResult()
    shop = build_shop(masters, _absent_from_reserved(reserved, masters, say))
    pins = _pins(frozen, batch_by_key, masters, say)

    # The progress hook is called with metrics=None for a candidate that could not
    # be scheduled at all — a NORMAL event in this search (an infeasible crew
    # genome), not an error. The app only wants the running count, and its own
    # ``best`` is recomputed at finalize, so the metrics object is deliberately not
    # forwarded: it is roster_engine's type, not the dict every caller expects.
    prog = (lambda evals, _metrics: on_progress(evals, None)) if on_progress else None

    res = roster_search.optimize(jobs, shop, _resolved(config),
                                 overlap=_overlap(config),
                                 budget_evals=int(budget_evals), seed=int(seed),
                                 on_eval=prog, should_cancel=should_cancel,
                                 frozen=pins)
    ordered = [batch_by_key[k] for k in res.sequence if k in batch_by_key]
    if not ordered:
        return OptimizeResult(evals=res.evaluations, cancelled=res.cancelled)
    crew = dict(res.crew_rank or {})
    # Measure the winner by REPLAYING it exactly as the app will: same sequence,
    # same crew, same frozen pins, same reservations. Anything else re-opens the
    # 2026-07-25 "the panel promised a plan the apply did not reproduce" gap.
    from dataclasses import replace as _replace
    winner = run(ordered, config=_replace(config, crew_rank=crew), masters=masters,
                 reserved=reserved, frozen=frozen)
    # ``or date.today()`` — the same fallback ``new_engine.optimize_sequence``
    # carries. A None here silently measured every candidate's lateness against
    # nothing.
    plan_start = getattr(config, "plan_start_date", None) or date.today()
    metrics = plan_metrics(
        winner, so_lines, plan_start,
        ceiling_days=getattr(config, "worst_ceiling_days", None),
        with_distribution=True,
        promise_slack_days=getattr(config, "committed_promise_slack_days", 3))
    return OptimizeResult(ranks=ranks_for(ordered), best=metrics,
                          evals=res.evaluations, improved=True,
                          cancelled=res.cancelled, crew_rank=crew)


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None,
                   frozen=None, **kw):
    """Local fallback for "Start deep search": run the sequence+crew search once
    per overlap contender and keep the best plan. The cloud path fans the SAME
    contenders across Actions shards, so the two agree by construction.

    Fair-contest contract, unchanged from the classic sweep: every contender gets
    the same depth, the current setting runs first (an early Stop leaves it fully
    searched) and wins exact ties.
    """
    from dataclasses import replace as _replace

    from engine.optimizer import (ROSTER_OVERLAP_CANDIDATES, OptimizeResult,
                                  SweepResult, score, sweep_contenders)

    current = getattr(config, "overlap_percent", None)
    lineup = sweep_contenders(current, ROSTER_OVERLAP_CANDIDATES)
    each = max(1, int(budget_evals) // max(1, len(lineup)))

    spent = {"n": 0}
    table, cancelled = [], False
    best = None                      # (score, overlap, OptimizeResult)

    def _offset(cb):
        if cb is None:
            return None
        return lambda evals, metrics: cb(spent["n"] + evals, metrics)

    for overlap in lineup:
        if should_cancel and should_cancel():
            cancelled = True
            break
        res = optimize_sequence(so_lines, _replace(config, overlap_percent=int(overlap)),
                                masters, reserved=base_reserved, budget_evals=each,
                                seed=seed, on_progress=_offset(on_progress),
                                should_cancel=should_cancel, frozen=frozen)
        spent["n"] += res.evals
        cancelled = cancelled or res.cancelled
        table.append({"overlap": int(overlap), "eligible": True,
                      "best": res.best, "evals": res.evals})
        if res.best is None:
            continue
        row = (score(res.best), int(overlap), res)
        if best is None or row[0] < best[0]:     # strict: an exact tie keeps first
            best = row

    if best is None:
        return SweepResult(overlap_percent=int(current or 0), knob="overlap_percent",
                           result=OptimizeResult(evals=spent["n"], cancelled=cancelled,
                                                 best=None),
                           table=table, evals=spent["n"], cancelled=cancelled)
    _sc, overlap, res = best
    return SweepResult(overlap_percent=overlap, knob="overlap_percent",
                       flexible_machines=False, result=res, table=table,
                       evals=spent["n"], cancelled=cancelled,
                       crew_rank=dict(res.crew_rank or {}))


def _resolved(config):
    """``plan_start_date`` as a real date. ``None`` means "auto: start from today
    (IST)" and the API boundary normally resolves it (``api._resolve_config``);
    this is the same ``or date.today()`` fallback ``new_engine.optimize_sequence``
    already carries, for the paths that reach an engine without going through it.
    Returns ``config`` untouched when it is already resolved, so no plan moves."""
    if getattr(config, "plan_start_date", None) is not None:
        return config
    try:
        return dataclasses.replace(config, plan_start_date=date.today())
    except TypeError:                      # not a dataclass (a test double)
        return config


def _report_dropped(plan, batch_by_key, say):
    """Name every order the engine could not place. It is in NO plan — no bar on
    the Gantt, no row in the delay report — and a silently dropped order is this
    codebase's most expensive recurring defect class, so it is reported through
    the same ``notes`` channel ``build_jobs`` reports its NO_ROUTING skips on.

    The plan itself is still returned: one machine with nobody qualified for it in
    Settings must not cost every OTHER order its schedule (CLAUDE.md principle
    5(a)). ``roster_engine`` raises instead when NOTHING could be placed.
    """
    dropped = list(getattr(plan, "dropped", ()) or ())
    if not dropped:
        return
    say.append(
        "roster engine: %d order(s) could NOT be scheduled and are in no plan — %s%s"
        % (len(dropped),
           "; ".join("%s (%s) blocked at step %s '%s' on %s — %s"
                     % (key, _order_label(batch_by_key.get(key)), seq, name,
                        "/".join(options) or "(no machine)", why)
                     for key, seq, name, options, why in dropped[:5]),
           "" if len(dropped) <= 5 else "; …"))


def _order_label(batch) -> str:
    if batch is None:
        return "?"
    refs = ", ".join(str(so) for so in _so_refs(batch)[:3])
    return "%s%s" % (getattr(batch, "item_code", "?"), " / " + refs if refs else "")


def _rule_error(err) -> RuleError:
    """``roster_engine`` fails loud with its own ``Unschedulable`` — it knows
    nothing about this app and must not, so it cannot raise ``RuleError`` itself.
    Translating it here is the whole reason this file exists.

    Why it matters that it is translated at all: ``pipeline.run_rule`` catches
    ``RuleError`` and nothing else. An escaping ``RuntimeError`` unwinds
    ``run_forward`` entirely — the trace Rules 1-3 filled in is discarded, every
    per-rule tab goes blank and ``POST /run`` returns a 500 with nothing to show
    the planner. CLAUDE.md principle 5(b): a rule-level contract violation is
    typed and LOCALIZED, so the frontend can say exactly where it broke. The same
    argument is why ``PlanStartMissing`` (a ``ValueError``, so ``except
    Unschedulable`` never saw it) is caught here too.

    This now means what it says: the whole book is unschedulable. A single
    unstaffable machine — including the documented PROVISIONAL one, named by a
    routing before the Machine master has caught up — degrades instead, dropping
    its own orders and reporting them (``_report_dropped``).
    """
    blocked = getattr(err, "blocked", ()) or ()
    record_id = str(blocked[0][0]) if blocked else "-"
    return RuleError("rule6", record_id, str(err))


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
    who = row_value(row, "so_no") or row_value(row, "order_key") or "?"
    item = row_value(row, "item_code") or ""
    seq = row_value(row, "op_seq", "process_seq")
    mid = row_value(row, "machine_id", "machine") or "?"
    return "%s%s step %s on %s" % (who, "/" + str(item) if item else "", seq, mid)


def _seq_for(row, batch, masters):
    """The routing step this row pins. Trust ``op_seq``; fall back to matching the
    routing by normalised process name, the way ``new_engine._ppc_frozen`` does."""
    raw = row_value(row, "op_seq", "process_seq")
    if raw is not None:
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, "step %r is not a number" % (raw,)
    want = normalize_process_name(row_value(row, "process", "process_name") or "")
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
    conflicts: list = []
    for row in rows:
        key = row_value(row, "order_key", "job_key")
        key = str(key) if key is not None and str(key) in batch_by_key else None
        if key is None:
            key = so_to_key.get((str(row_value(row, "so_no") or ""),
                                 str(row_value(row, "item_code") or "")))
        if key is None:
            unmapped.append((row, "no batch in this plan covers it"))
            continue
        seq, why = _seq_for(row, batch_by_key[key], masters)
        if seq is None:
            unmapped.append((row, why))
            continue
        mid = normalize_resource_id(row_value(row, "machine_id", "machine") or "")
        if not mid:
            unmapped.append((row, "the row names no machine"))
            continue
        rank = pin_rank(row, mid)
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
        if current is None:
            best[(key, seq)] = (rank, pin)
            continue
        # Two rows for ONE operation. Same machine -> a duplicate, the ordinary
        # clubbed-SO-lines case. DIFFERENT machines -> a genuine data conflict:
        # the last plan cannot have had one operation in two chucks, so something
        # upstream disagrees with itself. Reporting that as merely "superseded"
        # attributes a cause nobody checked (2026-08-09).
        kept, loser = (rank, pin), current
        if rank >= current[0]:
            kept, loser = current, (rank, pin)
        if kept[1]["machine_id"] != loser[1]["machine_id"]:
            conflicts.append((loser[1], kept[1]["machine_id"]))
        else:
            superseded += 1
        best[(key, seq)] = kept

    if unmapped:
        say.append("frozen work: %d in-progress row(s) could not be matched to "
                   "this plan and were left to normal scheduling — %s"
                   % (len(unmapped),
                      "; ".join("%s (%s)" % (_describe(r), why)
                                for r, why in unmapped[:5])))
    if superseded:
        say.append("frozen work: %d row(s) describe an operation another row "
                   "already covers ON THE SAME MACHINE (clubbed SO lines on one "
                   "batch step); one machine each, as an operation runs once."
                   % superseded)
    if conflicts:
        say.append("frozen work: %d in-progress row(s) CONFLICT — they name a "
                   "different machine for an operation another row already "
                   "covers, which one operation cannot be on. The "
                   "earlier-started row was kept: %s"
                   % (len(conflicts),
                      "; ".join("%s (kept %s)" % (_describe(r), kept)
                                for r, kept in conflicts[:5])))
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
