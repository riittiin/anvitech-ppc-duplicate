"""Adapter — run the NEW scheduling engine behind the old build's scheduler seam.

The old build turns prioritized batches into a schedule through ONE dispatch point
(engine/pipeline.py `scheduler_for` -> `run(...)`). This module provides a `run` with the
SAME contract that internally drives the new operator-stable engine (vendored as the
`ppc_engine` package) and maps its output back to the old `ScheduleEntry` list — so the
entire old UI (Gantt, Analytics, Schedule, Orders, Capture-Actuals) keeps working
unchanged while the *scheduling brain* is the new one.

Why this is safe: the new loaders produce byte-identical machine ids / item codes /
operator names to the old loaders on the same workbook (verified: 26/26 machines, 98/98
routings, 19/19 operators). So the new engine's output references exactly the ids the old
view layer looks up. We therefore load the new masters natively from the stored workbook
(no masters translation) and only bridge three things:
    demand : old Batch   -> new Order
    config : old Config  -> new PlanConfig
    output : new Segment -> old ScheduleEntry (one entry per operation; the new engine's
             per-shift Segment split becomes the entry's `op_segments`).
"""

from __future__ import annotations

import hashlib
import io
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from engine import book_store
from engine.loaders import normalize_process_name
from engine.models import ScheduleEntry
from engine.optimizer import COMMITTED_PROMISE_WEIGHT

from ppc_engine.config import PlanConfig
from ppc_engine.domain.order import Order
from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders import load_all
from ppc_engine.scheduler import decode

# The lane strings the old Gantt/Analytics expect for non-machine steps.
_OS_LANE = "OS / Outsourced"
_OFF_LANE = "Off-machine"

# New masters parsed from the stored workbook, cached by content hash so the workbook is
# parsed once per upload (the optimizer replays `run` many times).
_MASTERS_CACHE: dict = {}


def _norm(name) -> str:
    """Canonical process-name key. MUST be the EXACT same normaliser the order book
    uses to key a batch's ``process_qty`` (``engine.loaders.normalize_process_name``:
    collapse whitespace + uppercase, but KEEP word breaks — 'cnc  first side' ->
    'CNC FIRST SIDE'). A different rule (e.g. stripping spaces) silently drops every
    multi-word step from ``process_remaining``, so re-plans after production would
    ignore finished progress on those steps and re-schedule them at full qty."""
    return normalize_process_name(name)


# Workbook bytes injected out-of-band (the cloud optimize worker has no store — it carries
# the workbook in its payload). When set, it wins over book_store; the web app leaves it None.
_OVERRIDE_BYTES = None


def set_masters_bytes(raw: bytes | None) -> None:
    """Feed the new engine the masters workbook directly (used by the cloud worker, which
    runs from a payload rather than the store). Pass None to clear and fall back to the store."""
    global _OVERRIDE_BYTES
    _OVERRIDE_BYTES = raw


def _new_masters(flexible: bool = False):
    """Load the new-engine Masters at the given machine flexibility from the injected
    bytes or the stored workbook. flexible=False -> Allotted-only options (today);
    True -> the Allotted+Suggested union. Cached by (workbook sha, flexible)."""
    raw = _OVERRIDE_BYTES if _OVERRIDE_BYTES is not None else book_store.load_masters_bytes()
    if not raw:
        raise RuntimeError("new_engine: no masters workbook available (store empty and none injected)")
    h = hashlib.sha256(raw).hexdigest()
    key = (h, bool(flexible))
    cached = _MASTERS_CACHE.get(key)
    if cached is None:
        # Keep both flexibilities of the CURRENT workbook; evict any other workbook.
        for k in [k for k in _MASTERS_CACHE if k[0] != h]:
            del _MASTERS_CACHE[k]
        cached = load_all(io.BytesIO(raw), flexible_machines=bool(flexible)).masters
        _MASTERS_CACHE[key] = cached
    return cached


def _apply_app_operators(new_masters, old_masters):
    """Replace the new-engine masters' operators (parsed from the WORKBOOK, which is
    now a FOSSIL) with the APP-OWNED operator set carried on ``old_masters``.

    Operators are owned by the app (added/edited/removed in Settings, each with a
    shift that holds every week until an admin changes it — no automatic rotation),
    NOT the Excel sheet — so the app table is authoritative for WHO exists and their
    machines + shift. The one thing the app table does not carry is ROLE (operator /
    helper / inspector), so role is inherited BY NAME from the workbook operators; a
    person the workbook never had gets their role inferred from their qualified
    machines' kinds (else OPERATOR). Machines, routings and calendar stay
    workbook-owned. The input masters is never mutated.

    ``old_masters is None`` (a bare caller that passes no app set, e.g. a unit test)
    keeps the workbook operators — back-compat. An explicit EMPTY operator list is
    honoured as "no operators" (e.g. the owner deleted everyone), never resurrected
    from the workbook."""
    if old_masters is None:
        return new_masters
    app_ops = getattr(old_masters, "operators", None)
    if app_ops is None:
        return new_masters
    from dataclasses import replace as _replace
    from collections import Counter
    from ppc_engine.domain.resources import Operator as _NewOp, Role, ROLE_FOR_KIND
    from ppc_engine.loaders.normalize import parse_machine_options, parse_shift

    role_by_name = {o.name: o.role for o in new_masters.operators}
    kind_by_machine = {mid: getattr(m, "kind", None)
                       for mid, m in new_masters.machines.items()}

    def _role_for(name, quals):
        if name in role_by_name:
            return role_by_name[name]               # inherit the workbook role by name
        # App-added person the workbook never had: infer from their machines' kinds.
        votes = Counter(ROLE_FOR_KIND[kind_by_machine[m]] for m in quals
                        if kind_by_machine.get(m) in ROLE_FOR_KIND)
        return votes.most_common(1)[0][0] if votes else Role.OPERATOR

    ops = []
    for a in app_ops:
        quals = frozenset(parse_machine_options(
            getattr(a, "preferred_machines_raw", "") or ""))
        ops.append(_NewOp(name=a.name, role=_role_for(a.name, quals),
                          qualified_machines=quals,
                          base_shift=parse_shift(getattr(a, "shift", "") or "")))
    return _replace(new_masters, operators=tuple(ops))


def _friday_on_or_before(d: date) -> date:
    return d - timedelta(days=(d.weekday() - 4) % 7)


def _with_absences(masters, reserved):
    """Return a per-plan copy of the new-engine Masters with the app's operator absences
    folded into the calendar's per-operator leave, so an absent operator is never assigned.
    ``reserved`` is the old ``{operator -> [(start_dt, end_dt), ...]}`` from
    optimize_service.absence_reservations. Cached masters are never mutated."""
    from dataclasses import replace as _replace
    if not reserved:
        return masters
    extra: dict[str, set] = {}
    for op, intervals in reserved.items():
        days: set = set()
        for start, end in intervals:
            d = start.date()
            while d < end.date():
                days.add(d)
                d += timedelta(days=1)
        if days:
            extra[op] = days
    if not extra:
        return masters
    cal = masters.calendar
    merged = dict(getattr(cal, "leaves", {}) or {})
    for op, days in extra.items():
        merged[op] = frozenset(merged.get(op, frozenset()) | days)
    return _replace(masters, calendar=_replace(cal, leaves=merged))


def _plan_config(config) -> PlanConfig:
    """Translate the old Config into a new PlanConfig (shift hours, setup, overlap,
    plan start). Consolidation is 0 — the old Rule 1 already consolidated the batches."""
    start = getattr(config, "plan_start_date", None) or date.today()
    h = int(getattr(config, "first_shift_start_hour", 8))
    plan_start = datetime(start.year, start.month, start.day, h, 0)
    # Auto-mode floor (2026-08-03): never start in the past — a late run rolls the start to
    # the next full hour (set by api._resolve_config). Fixed-date/testing plans carry no
    # floor and keep the 08:00 start. plan_start = max(08:00-of-date, floor).
    floor_iso = getattr(config, "plan_start_floor", None)
    if floor_iso:
        try:
            floor = datetime.fromisoformat(floor_iso)
            if floor > plan_start:
                plan_start = floor
        except ValueError:
            pass
    # The new engine's overlap is always a fraction (0.0 = sequential). Read it straight
    # from overlap_percent, IGNORING the old 'overlap_mode' switch (the new engine has no
    # such mode) — otherwise a tuned or saved overlap silently has no effect. Clamp 0..0.95.
    overlap = min(0.95, max(0.0, float(getattr(config, "overlap_percent", 0)) / 100.0))
    return PlanConfig(
        plan_start=plan_start,
        # No shift rotation (2026-08-05): the shift an admin sets in Settings is the
        # shift the planner uses, every week. `week_anchor=None` is the engine's own
        # no-rotation path (ppc_engine/worktime.py: a None anchor returns base_shift
        # unchanged), so ppc_engine itself needs no change.
        week_anchor=None,
        first_start=time(int(getattr(config, "first_shift_start_hour", 8)), 0),
        first_end=time(int(getattr(config, "first_shift_end_hour", 19)), 0),
        second_start=time(int(getattr(config, "first_shift_end_hour", 19)), 0),
        second_end=time(int(getattr(config, "second_shift_end_hour", 5)), 0),
        setup_min=float(getattr(config, "setup_time_min", 90)),
        overlap=overlap,
        consolidation_window=0.0,
        ceiling_days=getattr(config, "worst_ceiling_days", None),
        committed_promise_slack_days=float(getattr(config, "committed_promise_slack_days", 3)),
        committed_promise_weight=COMMITTED_PROMISE_WEIGHT,
        # `Config.split_parallel` is deliberately NOT forwarded: parallel splitting
        # measured WORSE on the live book (see PlanConfig.split_enabled). Forwarding
        # it would silently degrade every plan, since the live config has it on.
        #
        # Settings' "balance operator load" — read only by the CLASSIC engine's
        # rule6_allocate, so it did nothing under scheduler='new'. Without it the
        # board uses "scarce", whose tie-break between equally-qualified people is
        # their NAME: on the live book Rohan Chakane ran at 90.4% and Sidhu Singe at
        # 81.6% purely because "Rohan" sorts first. "balanced" picks the least-loaded
        # instead (measured: Swapnil 10.6% -> 43.0%, Narayan 2.4% -> 16.5%, spread
        # 88 -> 74 points, late-days unchanged at 397 -> 398).
        operator_pick=("balanced" if getattr(config, "balance_operator_load", False)
                       else "scarce"),
    )


def _op_has_no_runnable_machine(op, op_qty, masters) -> bool:
    """True iff this operation would reach the scheduler's in-house branch with NO
    runnable machine. The decoder raises 'no runnable machine' (a RuntimeError that
    would 500 the WHOLE plan) for an in-house op — one that is NOT a DISPATCH/OUTSOURCED
    milestone and still has remaining work (``dur > 0``) — when none of its machine
    options both EXISTS in the master AND has a qualified operator. That happens with
    incomplete master data (e.g. a provisional machine referenced by a routing but not
    yet given operators). Mirrors the scheduler's own branch order in
    ppc_engine/scheduler/flow_scheduler.py:_place_operation."""
    if op.kind in (OperationKind.DISPATCH, OperationKind.OUTSOURCED):
        return False                     # milestone / off-site: no machine needed
    if op_qty <= 0:
        return False                     # already finished -> zero-time milestone
    qualified = set()
    for o in masters.operators:
        qualified |= set(getattr(o, "qualified_machines", ()) or ())
    return not any(mid in masters.machines and mid in qualified
                   for mid in op.machine_options)


def _orders_from_batches(batches, masters):
    """Old Batch[] -> new Order[], plus an order-key -> batch index for mapping back.

    Each batch becomes one Order keyed by (batch_id, item_code) — unique per batch. A
    batch's per-process remaining (`process_qty`, keyed by normalised process name) is
    mapped to the new engine's `process_remaining` (keyed by operation seq) via the
    routing, so a partially-produced order re-plans each step at its own remaining."""
    orders, batch_by_key = [], {}
    for b in batches:
        # An item with no routing is not schedulable — skip it (it still shows in the order
        # book / validation report). The classic engine tolerated this; the new engine must
        # too, or one unrouted order would crash the whole plan (KeyError on the routing).
        if b.item_code not in masters.routings:
            continue
        key = (b.batch_id, b.item_code)
        routing = masters.routings[b.item_code]
        process_remaining = None
        if getattr(b, "process_qty", None):
            process_remaining = {
                op.seq: int(round(b.process_qty[_norm(op.name)]))
                for op in routing.operations
                if _norm(op.name) in b.process_qty
            }

        # Forgiving-master rule (CLAUDE.md: never stop the pipeline for incomplete
        # master data): if any still-to-run in-house step has no machine that both
        # exists AND has a qualified operator, the decoder would raise and 500 the
        # entire plan. Skip THIS order (it still shows in the order book / report),
        # exactly as an unrouted order is skipped, so every other order still plans.
        def _op_qty(op):
            if process_remaining is not None:
                return process_remaining.get(op.seq, int(round(b.qty)))
            return int(round(b.qty))
        if any(_op_has_no_runnable_machine(op, _op_qty(op), masters)
               for op in routing.operations):
            continue

        promise_date = b.promised_date if getattr(b, "commitment", "open") == "committed" else None
        orders.append(Order(
            so_no=b.batch_id, item_code=b.item_code, item_name=b.item_name,
            qty=int(round(b.qty)), due_date=b.so_delivery_date,
            process_remaining=process_remaining,
            promise_date=promise_date,
        ))
        batch_by_key[key] = b
    return orders, batch_by_key


def _ppc_frozen(rows, orders, batch_by_key, masters):
    """Map app-level frozen rows -> ppc FrozenOp[] for decode. Each row is
    {so_no, item_code, process, op_seq, machine, operator, remaining_qty, prev_start-iso}.
    A row maps to the scheduled batch whose source SOs include ``so_no`` (batch_id ==
    ppc order_key[0]); its op_seq is taken from the row (or resolved via the routing by
    normalised process name). Rows that don't map to a scheduled order, have an unknown/
    OS machine, have a remaining_qty<=0, or have a malformed remaining_qty/prev_start
    (non-numeric, None, missing) are dropped -- never raised, so one bad row can't
    take down the whole call (and, once wired into a Plan action, the whole plan).

    **A frozen row pins WHERE and WHEN, never HOW MUCH** (2026-08-11 live bug,
    director escalation). A row's ``remaining_qty`` is that ONE SO line's remaining;
    the op it pins is a BATCH operation that Rule 1 may have clubbed several SO lines
    into. ``_preplace_frozen`` lays exactly ``FrozenOp.remaining_qty`` pieces and then
    advances the order past that op, so anything the row under-counted is never
    scheduled at all: a part-finished SO (88 left) clubbed with an untouched one (281)
    ran 88 pieces of CNC FIRST SIDE while every downstream step ran the full 535.
    So the qty comes from the BATCH -- ``Order.process_remaining``, the exact
    expression the main decode loop uses (``flow_scheduler._place_operation``) -- and
    the rows are collapsed to ONE FrozenOp per (batch, op): an operation runs once, on
    one machine. Machine/operator/prev_start come from the row that started earliest."""
    from datetime import datetime
    from ppc_engine.scheduler import FrozenOp
    # Reverse index: (so_no, item_code) -> order_key of the batch that covers it.
    so_to_key = {}
    for key, batch in batch_by_key.items():
        for so in (getattr(batch, "source_so_refs", None) or []):
            so_to_key[(so, batch.item_code)] = key
    order_by_key = {o.key: o for o in orders}
    pinned = {}                      # (order_key, op_seq) -> (prev_start, so_no, mid, operator)
    for r in rows or []:
        key = so_to_key.get((r.get("so_no"), r.get("item_code")))
        if key is None or key not in order_by_key:
            continue
        mid = r.get("machine")
        if not mid or mid not in masters.machines:   # unknown / OS / off-lane
            continue
        try:
            if int(round(float(r.get("remaining_qty", 0)))) <= 0:
                continue             # this line is finished at this step -> nothing to pin
        except (TypeError, ValueError):
            continue
        # Resolve op_seq: trust the row, else match the routing by normalised name.
        op_seq = r.get("op_seq")
        if op_seq is None:
            want = _norm(r.get("process", ""))
            op_seq = next((op.seq for op in masters.routings[order_by_key[key].item_code].operations
                           if _norm(op.name) == want), None)
            if op_seq is None:
                continue
        try:
            prev_start = datetime.fromisoformat(r["prev_start"])
        except (KeyError, ValueError, TypeError):
            continue
        # Several clubbed SO lines of one batch can be in progress on the same step.
        # That is still ONE operation: keep the earliest-started row's pin (so_no
        # breaks a tie, so the choice can't depend on dict order).
        stamp = (prev_start, str(r.get("so_no") or ""))
        cur = pinned.get((key, int(op_seq)))
        if cur is None or stamp < cur[0]:
            pinned[(key, int(op_seq))] = (stamp, mid, r.get("operator", "") or "")

    out = []
    for (key, op_seq), (stamp, mid, operator) in pinned.items():
        order = order_by_key[key]
        # How much work is left on this step is the BATCH's business, not the row's.
        pr = getattr(order, "process_remaining", None)
        qty = int(round(pr.get(op_seq, order.qty))) if pr is not None else int(round(order.qty))
        if qty <= 0:
            continue                 # step already finished for the whole batch
        out.append(FrozenOp(order_key=key, op_seq=op_seq, machine_id=mid,
                            operator=operator, remaining_qty=qty, prev_start=stamp[0]))
    out.sort(key=lambda f: (f.prev_start, f.order_key, f.op_seq))
    return out


def _machine_for(kind, machine_id) -> str:
    """The old Gantt/Analytics machine field: canonical id for in-house steps, the OS
    lane for outsourced, the off-machine lane for a resource-less in-house step."""
    if kind == OperationKind.OUTSOURCED:
        return _OS_LANE
    if machine_id:
        return machine_id
    return _OFF_LANE


def qualification_violations(entries, new_masters):
    """Every place the schedule puts an operator on a machine they are NOT assigned to
    in Settings. Pure; returns ``[{"kind", "ref", "message"}]``, empty when clean.

    Defense in depth for the 2026-08-03 / 2026-08-07 class of bugs: the scheduler is
    supposed to make this impossible, but it silently shipped twice — once via frozen
    in-progress ops re-pinning a de-qualified operator, once via a role gate that threw
    the admin's assignment away. An invariant that is CHECKED beats one that is merely
    intended. Tests assert it is empty; the API surfaces it as a non-blocking report row
    rather than breaking a live plan."""
    quals = {o.name: set(o.qualified_machines) for o in new_masters.operators}
    seen, out = set(), []
    for e in entries:
        if e.machine in (_OS_LANE, _OFF_LANE):
            continue
        for (_s, _e, op) in (getattr(e, "op_segments", None) or []):
            if not op or (op, e.machine) in seen:
                continue
            if e.machine not in quals.get(op, set()):
                seen.add((op, e.machine))
                out.append({
                    "kind": "OPERATOR_NOT_QUALIFIED", "ref": f"{op} / {e.machine}",
                    "message": (f"the plan plans operator '{op}' on machine "
                                f"'{e.machine}', which is not in their machine list "
                                f"under Settings > Operators & shifts")})
    return out


def batch_quantity_violations(entries, batches):
    """Every step the plan schedules FEWER pieces on than the order book says it owes.

    Pure; returns ``[{"kind", "ref", "message"}]``, empty when clean. Sibling of
    ``routing_order_violations`` / ``qualification_violations`` and surfaced the same
    way (``api._report_for_book``, non-blocking — a live plan must never break).

    A batch owes ``batch.process_qty[step]`` on each step (Rule 1's per-step remaining
    across every SO line it clubbed), or ``batch.qty`` where the book records no
    progress. Scheduling less than that means pieces the customer ordered are in no
    plan at all — silently, since every OTHER step still shows the full quantity. That
    is the 2026-08-11 live bug (a frozen op ran only the punched SO line's remainder);
    checked here so the next code path that does it is caught instead of shipped.

    An operation's pieces can be published either as ONE entry carrying the whole qty
    (the new engine, whose blocks each repeat the operation's qty) or as several
    entries that SUM to it (the classic engine's parallel split), so a step is short
    only when BOTH readings fall below what is owed."""
    owed, refs = {}, {}
    for b in batches or []:
        owed[b.batch_id] = (getattr(b, "process_qty", None) or {}, float(b.qty))
        refs[b.batch_id] = ", ".join(getattr(b, "source_so_refs", None) or []) or b.batch_id
    biggest, total, name_of = {}, {}, {}
    for e in entries or []:
        k = (e.batch_id, e.process_seq)
        biggest[k] = max(biggest.get(k, 0.0), float(e.qty))
        total[k] = total.get(k, 0.0) + float(e.qty)
        name_of[k] = e.process_name
    out = []
    for k in sorted(biggest, key=lambda k: (k[0], k[1])):
        bid, seq = k
        if bid not in owed:
            continue                       # entry from a batch we weren't given
        pq, bqty = owed[bid]
        want = float(pq.get(_norm(name_of[k]), bqty))
        if biggest[k] + 0.5 >= want or total[k] + 0.5 >= want:
            continue
        out.append({
            "kind": "BATCH_QTY_SHORT",
            "ref": f"{refs[bid]} / {name_of[k]}",
            "message": (f"the plan runs {biggest[k]:.0f} pieces of '{name_of[k]}' but "
                        f"{want:.0f} are still to be made ({refs[bid]}); "
                        f"{want - biggest[k]:.0f} pieces are in no plan")})
    return out


def routing_order_violations(entries, masters):
    """Every place the schedule runs an operation before the step that FEEDS it.

    Pure; returns ``[{"kind", "ref", "message"}]``, empty when clean. Per
    ``(SO number, item code)``, for consecutive steps ``a`` then ``b`` of the item's
    routing in Item's Process Master:

        start(b) >  start(a)      b cannot begin before, or with, a
        end(b)   >= end(a)        b cannot finish before a finishes

    Deliberately NOT flagged, because the engine means both: Rule 5 **overlap** lets
    ``b`` start while ``a`` is still cutting, and overlap **pacing** stretches a fast
    ``b`` to end exactly with ``a``. An equal start is only allowed after a
    zero-duration step (an OS / off-machine milestone produces no pieces, so nothing
    has to wait for it).

    Sibling of `qualification_violations`, and it exists for the same reason: the
    scheduler is supposed to make this impossible, and on a clean book it does — but
    once work was IN PROGRESS, `flow_scheduler._preplace_frozen` pinned each
    part-finished op at its own machine's first free slot with no reference to the
    order's own predecessor, and shipped a schedule that ran CNC SECOND SIDE two days
    before CNC FIRST SIDE (live 2026-08-09, 63 inversions over 21 of 68 real orders).
    An invariant that is CHECKED beats one that is merely intended: tests assert this
    is empty, and the API surfaces it as a non-blocking report row rather than
    breaking a live plan."""
    pos_of = {}
    for item, routing in (getattr(masters, "routings", None) or {}).items():
        for i, p in enumerate(routing.processes):
            pos_of[(item, p.seq)] = (i, p.name)

    # One span per (order, routing step): a step split across machines or shifts is
    # still ONE step, so take its full extent.
    spans = defaultdict(dict)
    for e in entries:
        found = pos_of.get((e.item_code, e.process_seq))
        if found is None:
            continue                      # step not in the master — nothing to order by
        i, name = found
        for so in (e.so_refs or []):
            cur = spans[(so, e.item_code)].get(i)
            if cur is None:
                spans[(so, e.item_code)][i] = [name, e.start, e.end]
            else:
                cur[1] = min(cur[1], e.start)
                cur[2] = max(cur[2], e.end)

    out = []
    for (so, item), by_pos in sorted(spans.items()):
        steps = sorted(by_pos.items())
        for (_ia, (na, sa, ea)), (_ib, (nb, sb, eb)) in zip(steps, steps[1:]):
            ref = f"{so} / {item}"
            if sb < sa:
                out.append({"kind": "ROUTING_ORDER_VIOLATION", "ref": ref,
                            "message": (f"'{nb}' starts before '{na}', the step that "
                                        f"feeds it ({sb:%d-%m %H:%M} vs "
                                        f"{sa:%d-%m %H:%M})")})
            elif sb == sa and ea > sa:
                out.append({"kind": "ROUTING_ORDER_VIOLATION", "ref": ref,
                            "message": (f"'{nb}' starts at the same instant as '{na}', "
                                        f"the step that feeds it ({sa:%d-%m %H:%M})")})
            if eb < ea:
                out.append({"kind": "ROUTING_ORDER_VIOLATION", "ref": ref,
                            "message": (f"'{nb}' finishes before '{na}', the step that "
                                        f"feeds it ({eb:%d-%m %H:%M} vs "
                                        f"{ea:%d-%m %H:%M})")})
    return out


def _entries_from_schedule(sched, batch_by_key):
    """New Segment[] -> old ScheduleEntry[]. One entry per operation; the new engine's
    per-shift segments become the entry's `op_segments` (the per-shift operator
    hand-offs the old Analytics reads). The DISPATCH milestone is dropped — the old view
    derives completion from the last real operation's end."""
    groups = defaultdict(list)
    for s in sched.segments:
        groups[(s.order_key, s.op_seq)].append(s)

    # Every segment on each machine, so an operation's break can be classified. A
    # night / weekly off / shift change is NOT a break in the work — it happens every
    # day and must stay ONE entry, exactly as before. Only the machine actually
    # running ANOTHER job in between is a real interruption, and that only happens
    # once the idle-gap harvest splits a job to fill a hole (step 1 of
    # docs/superpowers/specs/2026-08-09-idle-gap-harvest-design.md). Nothing in
    # today's plans is interrupted, so this is a deliberate no-op until then.
    on_machine = defaultdict(list)
    for s in sched.segments:
        if s.machine_id is not None:
            on_machine[s.machine_id].append(s)

    def _blocks(segs):
        """An operation's segments split into continuously-worked blocks."""
        out = [[segs[0]]]
        for prev, cur in zip(segs, segs[1:]):
            interrupted = any(
                (o.order_key, o.op_seq) != (cur.order_key, cur.op_seq)
                and o.start < cur.start and o.end > prev.end
                for o in on_machine.get(cur.machine_id, ()))
            if interrupted:
                out.append([cur])
            else:
                out[-1].append(cur)
        return out

    entries = []
    for (order_key, op_seq), segs in groups.items():
        segs = sorted(segs, key=lambda s: s.start)
        if segs[0].kind == OperationKind.DISPATCH:
            continue
        batch = batch_by_key.get(order_key)
        for block in _blocks(segs):
            first = block[0]
            # Per BLOCK, never per operation: a split job's second block must publish
            # its own dates, its own pieces and its own machine time, or every sheet
            # reads the first block's numbers for both.
            occupancy_min = sum((s.end - s.start).total_seconds() for s in block) / 60.0
            operator = next((s.operator for s in block if s.operator), "")
            # OS steps are single milestone entries (no per-shift operator segments).
            op_segments = ([] if first.kind == OperationKind.OUTSOURCED
                           else [(s.start, s.end, s.operator or "") for s in block])
            entries.append(ScheduleEntry(
                batch_id=order_key[0],
                item_code=order_key[1],
                process_seq=op_seq,
                process_name=first.op_name,
                machine=_machine_for(first.kind, first.machine_id),
                qty=float(first.qty),
                occupancy_min=occupancy_min,
                start=min(s.start for s in block),
                end=max(s.end for s in block),
                notes="",
                so_refs=list(batch.source_so_refs) if batch else [],
                operator=operator,
                op_segments=op_segments,
            ))
    # PACE the DISPLAY span: with overlap the new engine lets a fast downstream op
    # finish its cutting before the slow step feeding it — physically impossible (the
    # pieces don't exist yet). Extend each op's `end` to >= its predecessor's paced end
    # so the Gantt/expected-completion never show a step finishing before its input.
    # ONLY the span (`end`) grows — `op_segments` (operator busy) and `occupancy_min`
    # (machine busy) are the real cutting time and stay untouched (span > occupancy,
    # exactly how the classic engine reports it).
    by_batch = defaultdict(list)
    for e in entries:
        by_batch[e.batch_id].append(e)
    for es in by_batch.values():
        es.sort(key=lambda e: e.process_seq)
        paced = None
        for e in es:
            if paced is not None and e.end < paced:
                e.end = paced
            paced = e.end

    entries.sort(key=lambda e: (e.start, e.batch_id, e.process_seq))
    return entries


# A fingerprint the API mixes into its plan-staleness signature
# (api/main.py:_inputs_signature), so a change to how the new engine allocates
# work (e.g. dropping automatic operator-shift rotation, 2026-08-05) correctly
# flags an applied optimization stale instead of replaying old ranks under new
# semantics behind a green banner.
# The scheduler's SEMANTICS version. Saved optimization ranks were scored under a
# specific placement policy, so ANY deploy that changes how operations are placed must
# bump this: `api._inputs_signature` folds it in, which flags the applied optimization
# stale, shows the "run Start deep search" banner, and lets the next Done click
# actually re-run a contest instead of skipping on "nothing changed". Forgetting it
# replays old ranks under new semantics behind a green banner.
# v3 (2026-08-09) = two placement changes in one day: the routing gate + piece-flow
# guard in `_preplace_frozen` (in-progress ops no longer run before the step feeding
# them) and first-fit gap backfill (`_first_fit_on_machine`).
# v5 (2026-08-11) = a frozen op runs the WHOLE clubbed batch's remaining pieces, not
# just the punched SO line's (`_ppc_frozen`) — real work moved, so ranks scored under
# the old semantics are stale.
SCHEDULER_FINGERPRINT = "new-engine-v5-frozen-batch-qty"


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, frozen=None, **kw):
    """Scheduler seam contract: prioritized `batches` -> list[ScheduleEntry], via the new
    operator-stable engine.

    The batches arrive already in priority order (Rules 1-3, or a saved optimization's
    rank map), so that order IS the sequence handed to the new decoder — the new engine
    schedules them in exactly the priority the rest of the pipeline decided.
    """
    if not batches:
        return []
    # Operators are APP-OWNED — overlay them onto the workbook masters (the sheet is a
    # fossil), so a delete/edit/rotation in Settings is what actually schedules.
    new_masters = _with_absences(_apply_app_operators(
        _new_masters(bool(getattr(config, "flexible_machines", False))), masters), reserved)
    orders, batch_by_key = _orders_from_batches(batches, new_masters)
    if not orders:
        return []
    # Sequence from the ROUTED orders only (unrouted batches were skipped above), preserving
    # the incoming priority order.
    sequence = [o.key for o in orders]
    ppc_frozen = _ppc_frozen(frozen, orders, batch_by_key, new_masters) if frozen else None
    sched = decode(orders, sequence, new_masters, _plan_config(config), frozen=ppc_frozen)
    return _entries_from_schedule(sched, batch_by_key)


def optimize_sequence(so_lines, config, masters, *, reserved=None, budget_evals=150,
                      seed=42, on_progress=None, should_cancel=None, frozen=None):
    """Sequence-only search for the NEW engine at the config's overlap. The cloud contest
    sweeps overlaps EXTERNALLY (one candidate per overlap) and calls this per candidate, so
    across candidates it becomes the full overlap × sequence contest — just distributed and
    at scale on GitHub Actions. Returns the old OptimizeResult so the contest/apply machinery
    is unchanged."""
    from engine.optimizer import OptimizeResult, plan_metrics, ranks_for
    from engine.rules import rule1_consolidate

    from ppc_engine.optimize import optimize as new_optimize

    # App-owned operators (the search must optimize against the SAME crew the plan runs).
    nm = _with_absences(_apply_app_operators(
        _new_masters(bool(getattr(config, "flexible_machines", False))), masters), reserved)
    cfg = _plan_config(config)
    plan_start = getattr(config, "plan_start_date", None) or date.today()
    batches = rule1_consolidate.run(so_lines, config)
    if not batches:
        return OptimizeResult()
    orders, batch_by_key = _orders_from_batches(batches, nm)
    ppc_frozen = _ppc_frozen(frozen, orders, batch_by_key, nm) if frozen else None
    # Report EVERY plan (on_eval), not just improvements, so the live counter climbs steadily.
    prog = (lambda evals, _sc: on_progress(evals, None)) if on_progress else None
    res = new_optimize(orders, nm, cfg, budget=int(budget_evals), seed=int(seed), on_eval=prog,
                       frozen=ppc_frozen, should_cancel=should_cancel)
    best_batches = [batch_by_key[k] for k in res.best_sequence if k in batch_by_key]
    ranks = ranks_for(best_batches)
    # Measure the winner against the SAME crew + reservations the plan actually runs.
    # (masters/reserved MUST be keyword args — run()'s 3rd positional is `notes`, so
    # `run(best_batches, config, masters)` silently scored the winner with masters=None
    # → the workbook's full operator sheet, promising a plan the app crew can't match.)
    winner_sched = run(best_batches, config=config, masters=masters, reserved=reserved, frozen=frozen)
    winner_metrics = plan_metrics(winner_sched, so_lines, plan_start,
                                  ceiling_days=getattr(config, "worst_ceiling_days", None),
                                  with_distribution=True,
                                  promise_slack_days=getattr(config, "committed_promise_slack_days", 3))
    return OptimizeResult(ranks=ranks, best=winner_metrics, evals=res.evaluations,
                          improved=True, cancelled=res.cancelled)


def tune(so_lines, config, masters, *, budget_per_eval=150, seed=42, on_step=None, reserved=None, frozen=None, should_cancel=None):
    """The CONTINUOUS overlap optimizer + sequence search (the 'atom optimizer').

    Golden-section search over the overlap value: it treats "best plan score achievable at
    overlap x" as a function of x and homes in on its true minimum — a CONTINUOUS value like
    0.78 / 0.82 / 0.913, not a fixed grid point. Each probe runs the full sequence search
    (comparing many plans), so it optimizes the overlap AND the job order together.

    ``on_step(cumulative_plans, best_score_so_far)`` is fired after every plan for a live
    tracker. Returns ``(ranks, best_overlap_percent, winner_metrics_old_space, plans)``.
    """
    from dataclasses import replace

    from engine.optimizer import plan_metrics, ranks_for
    from engine.rules import rule1_consolidate

    from ppc_engine.optimize import tune_overlap
    from ppc_engine.scheduler import decode

    # App-owned operators — same overlay as run()/optimize_sequence().
    new_masters = _with_absences(_apply_app_operators(
        _new_masters(bool(getattr(config, "flexible_machines", False))), masters), reserved)
    base = _plan_config(config)
    plan_start = getattr(config, "plan_start_date", None) or date.today()

    batches = rule1_consolidate.run(so_lines, config)
    if not batches:
        return {}, int(round(base.overlap * 100)), {}, 0
    orders, batch_by_key = _orders_from_batches(batches, new_masters)
    ppc_frozen = _ppc_frozen(frozen, orders, batch_by_key, new_masters) if frozen else None

    tr = tune_overlap(orders, new_masters, base, lo=0.5, hi=0.95, seeds=(int(seed),),
                      budget_per_eval=int(budget_per_eval), tol=0.01, coarse=5, on_step=on_step,
                      frozen=ppc_frozen, should_cancel=should_cancel)

    best_batches = [batch_by_key[k] for k in tr.best_sequence if k in batch_by_key]
    ranks = ranks_for(best_batches)
    # Report metrics at the APPLIED (integer-%) overlap, not the continuous best, so the
    # before/after panel matches the plan the app actually re-plans (no overstated gain).
    overlap_pct = int(round(tr.best_overlap * 100))
    won_cfg = replace(base, overlap=overlap_pct / 100.0)
    winner_metrics = plan_metrics(
        _entries_from_schedule(decode(orders, tr.best_sequence, new_masters, won_cfg, frozen=ppc_frozen), batch_by_key),
        so_lines, plan_start, ceiling_days=getattr(config, "worst_ceiling_days", None),
        with_distribution=True,
        promise_slack_days=getattr(config, "committed_promise_slack_days", 3))
    return ranks, overlap_pct, winner_metrics, tr.evaluations


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None, frozen=None, **kw):
    """Local fallback for 'Start deep search'. Runs the golden-section tune once per
    machine-set (Allotted-only, then Allotted+Suggested) and keeps the better plan by
    score — the third Optimize dimension. Returns the old SweepResult shape."""
    from dataclasses import replace
    from engine.optimizer import OptimizeResult, SweepResult, score

    per = max(15, int(budget_evals) // 10)
    best = None                       # (ranks, overlap_pct, metrics, plans, flexible)
    offset = {"n": 0}
    cancelled = False

    for flex in (False, True):
        def _step(plans, _best, _flex=flex):
            if on_progress:
                on_progress(offset["n"] + plans, (best or (None,) * 3)[2] if best else {})
        cfg = replace(config, flexible_machines=flex)
        ranks, overlap_pct, metrics, plans = tune(so_lines, cfg, masters,
                                                  budget_per_eval=per, seed=seed, on_step=_step,
                                                  reserved=base_reserved, frozen=frozen,
                                                  should_cancel=should_cancel)
        offset["n"] += plans
        if ranks and (best is None or score(metrics) < score(best[2])):
            best = (ranks, overlap_pct, metrics, plans, flex)
        # "Stop & keep best": the tune above already halted promptly (it polls
        # should_cancel per eval); don't start the next machine-set pass.
        if should_cancel and should_cancel():
            cancelled = True
            break

    if best is None:
        return SweepResult(overlap_percent=int(round(_plan_config(config).overlap * 100)),
                           knob="overlap", flexible_machines=False,
                           result=OptimizeResult(evals=0, best=None), table=[], evals=offset["n"],
                           cancelled=cancelled)
    ranks, overlap_pct, metrics, plans, flex = best
    result = OptimizeResult(ranks=ranks, best=metrics, evals=offset["n"], improved=True, cancelled=cancelled)
    return SweepResult(overlap_percent=overlap_pct, knob="overlap", flexible_machines=flex,
                       result=result, table=[], evals=offset["n"], cancelled=cancelled)
