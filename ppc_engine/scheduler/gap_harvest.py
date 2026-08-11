"""Harvest idle machine time — WITHOUT rescheduling anything.

Owner's rule (2026-08-09): a machine idle, with a free qualified operator, and work
pending, is not acceptable. Recover every hour of it.

Spec: docs/superpowers/specs/2026-08-09-idle-gap-harvest-design.md

**Why this is a post-pass and not a scheduling change.** Every earlier attempt re-ran
the dispatcher; changing one placement reshuffled the whole greedy cascade, other orders
lost their machines, and deliveries slipped — measured at ~40 late-days on the live book
(reverted in 7d0ed71). So this treats everything already placed as FROZEN. It only:

  * moves PART of a pending job into a hole that was going to be wasted, and
  * trims exactly that many pieces off the END of that job's later block.

Nothing is ever moved later. A block's start never moves; a block only gets shorter.
Therefore an order's completion can only come EARLIER or stay the same — a guarantee by
construction, which is what the rescheduling designs could never give. The test asserts
it per order, not in aggregate.

Physical rules honoured (never re-derived — the same helpers the scheduler uses):
working windows via ``iter_windows``; operator qualification, shift and availability
mirrored from ``_lay_frozen``; the routing predecessor must have ENDED; whole pieces
only; CNC/VMC pay a fresh setup per engagement.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from ppc_engine.domain.routing import OperationKind
from ppc_engine.scheduler.schedule import Schedule, Segment
from ppc_engine.worktime import effective_shift, iter_windows

_EPS = 1e-9
# A hole is not worth taking if it can only pay for setup and a token few minutes.
_MIN_USEFUL_MIN = 30.0


def _merge(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _gaps(a, b, busy):
    out, cur = [], a
    for s, e in _merge(busy):
        if s > cur:
            out.append((cur, min(s, b)))
        cur = max(cur, e)
    if cur < b:
        out.append((cur, b))
    return [(x, y) for x, y in out if y > x]


def _free(ivs, a, b):
    return not any(s < b and a < e for s, e in ivs)


def harvest(schedule: Schedule, masters, config) -> Schedule:
    """Fill idle machine time with pending work, moving nothing later."""
    segs = list(schedule.segments)
    real = [s for s in segs if s.machine_id is not None and s.end > s.start]
    if not real:
        return schedule

    horizon = max(s.end for s in segs)
    start_at = min(s.start for s in segs)

    # --- what each operation is, and what it still owes ---------------------- #
    ops = defaultdict(list)                       # (order_key, op_seq) -> [segment]
    for s in segs:
        ops[(s.order_key, s.op_seq)].append(s)
    for v in ops.values():
        v.sort(key=lambda s: s.start)

    routing_pos, op_of = {}, {}
    for item, rt in masters.routings.items():
        for i, o in enumerate(rt.operations):
            routing_pos[(item, o.seq)] = i
            op_of[(item, o.seq)] = o

    def pred_end(key):
        (order_key, op_seq) = key
        item = order_key[1]
        me = routing_pos.get((item, op_seq))
        if me is None:
            return None
        best = None
        for (ok, sq), lst in ops.items():
            if ok != order_key:
                continue
            p = routing_pos.get((item, sq))
            if p is None or p >= me:
                continue
            e = max(x.end for x in lst)
            best = e if best is None or e > best else best
        return best

    machine_busy = defaultdict(list)
    for s in real:
        machine_busy[s.machine_id].append((s.start, s.end))
    machine_busy = {k: _merge(v) for k, v in machine_busy.items()}

    op_busy = defaultdict(list)
    for s in segs:
        if s.operator:
            op_busy[s.operator].append((s.start, s.end))
    op_busy = defaultdict(list, {k: _merge(v) for k, v in op_busy.items()})

    # --- Rule 1: one operator per machine per shift, and nobody runs two --------- #
    # Who already owns each machine-shift in the plan, and which machine each person is
    # already committed to that shift. A hole on CNC1 must be filled by CNC1's operator
    # for that shift — never by someone hopping over from CNC4 mid-shift, which is the
    # exact thing this engine exists to prevent (RULES.md Rule 1).
    owner_of, committed = {}, {}
    segs_on = defaultdict(list)
    for s in real:
        if s.operator:
            segs_on[s.machine_id].append(s)
    for mid, lst in segs_on.items():
        machine = masters.machines.get(mid)
        if machine is None:
            continue
        for win in iter_windows(machine, start_at, masters.calendar, config):
            if win.start >= horizon:
                break
            for s in lst:
                if s.start < win.end and win.start < s.end:
                    owner_of.setdefault((win.shift_date, win.shift, mid), s.operator)
                    committed.setdefault((win.shift_date, win.shift, s.operator), mid)

    def _operator_for(mid, win, a, b):
        """CNC1's shift operator, or — if nobody is on it — a qualified person not
        already committed to another machine that shift."""
        key = (win.shift_date, win.shift, mid)
        owner = owner_of.get(key)
        if owner is not None:
            ok = (masters.calendar.is_operator_available(owner, win.shift_date)
                  and _free(op_busy[owner], a, b))
            return owner if ok else None
        for o in masters.operators:
            if (mid in o.qualified_machines
                    and effective_shift(o, win.shift_date, config) == win.shift
                    and masters.calendar.is_operator_available(o.name, win.shift_date)
                    and committed.get((win.shift_date, win.shift, o.name), mid) == mid
                    and _free(op_busy[o.name], a, b)):
                return o.name
        return None

    # --- the holes, oldest first --------------------------------------------- #
    holes = []
    for mid, busy in machine_busy.items():
        machine = masters.machines.get(mid)
        if machine is None:
            continue
        for win in iter_windows(machine, start_at, masters.calendar, config):
            if win.start >= horizon:
                break
            a, b = max(win.start, start_at), min(win.end, horizon)
            if b <= a:
                continue
            for ga, gb in _gaps(a, b, busy):
                if (gb - ga).total_seconds() / 60.0 >= _MIN_USEFUL_MIN:
                    holes.append((ga, gb, mid, win))
    holes.sort(key=lambda h: (h[0], h[2]))

    added, trimmed_min = [], defaultdict(float)
    for ga, gb, mid, win in holes:
        cur = ga
        # Keep filling THIS hole until nothing more fits. Taking one job and moving on
        # left most of an 8-hour hole empty.
        while (gb - cur).total_seconds() / 60.0 >= _MIN_USEFUL_MIN:
            if not _free(machine_busy.get(mid, []), cur, gb):
                break                                  # filled by an earlier pass
            avail = (gb - cur).total_seconds() / 60.0

            who = _operator_for(mid, win, cur, gb)
            if who is None:
                break

            best = None
            for key, lst in ops.items():
                if lst[0].kind == OperationKind.DISPATCH or lst[0].machine_id is None:
                    continue
                if min(x.start for x in lst) <= gb:    # already runs at/before the hole
                    continue
                op = op_of.get((key[0][1], key[1]))
                # ANY machine the ROUTING allows — not just the one this job happens to
                # be planned on. An idle operator beside an idle machine they can run is
                # the thing being fixed; which machine the plan picked is not a reason to
                # leave both sitting (owner, 2026-08-09).
                if op is None or not op.cycle_min or mid not in (op.machine_options or ()):
                    continue
                pe = pred_end(key)
                if pe is not None and pe > cur:
                    continue
                left = sum((x.end - x.start).total_seconds() / 60.0
                           for x in lst) - trimmed_min[key]
                if left <= _EPS:
                    continue
                if best is None or min(x.start for x in lst) < best[1]:
                    best = (key, min(x.start for x in lst), op, left)
            if best is None:
                break

            key, _st, op, left = best
            setup = config.setup_min if op.kind == OperationKind.MACHINING else 0.0
            pieces = int((avail - setup) // op.cycle_min)
            if pieces < 1:
                break
            take = min(pieces * op.cycle_min, left)
            if take < _EPS or take + setup > avail + _EPS:
                break
            used = take + setup
            s0, s1 = cur, cur + timedelta(minutes=used)
            first = ops[key][0]
            added.append(Segment(key[0], key[1], first.op_name, first.kind, mid, who,
                                 s0, s1, int(round(take / op.cycle_min))))
            trimmed_min[key] += take
            machine_busy[mid] = _merge(machine_busy.get(mid, []) + [(s0, s1)])
            op_busy[who] = _merge(op_busy[who] + [(s0, s1)])
            owner_of.setdefault((win.shift_date, win.shift, mid), who)
            committed.setdefault((win.shift_date, win.shift, who), mid)
            cur = s1

    if not added:
        return schedule

    # --- trim the SAME work off the END of each harvested op's later block ---- #
    out = []
    for key, lst in ops.items():
        cut = trimmed_min.get(key, 0.0)
        if cut <= _EPS:
            out.extend(lst)
            continue
        op = op_of[(key[0][1], key[1])]
        keep = []
        for s in reversed(lst):
            dur = (s.end - s.start).total_seconds() / 60.0
            if cut <= _EPS:
                keep.append(s)
            elif cut >= dur - _EPS:
                cut -= dur                              # whole segment absorbed
            else:
                keep.append(Segment(s.order_key, s.op_seq, s.op_name, s.kind,
                                    s.machine_id, s.operator, s.start,
                                    s.end - timedelta(minutes=cut),
                                    s.qty))
                cut = 0.0
        keep.reverse()
        left_min = sum((s.end - s.start).total_seconds() / 60.0 for s in keep)
        qty = int(round(left_min / op.cycle_min)) if op.cycle_min else 0
        out.extend(Segment(s.order_key, s.op_seq, s.op_name, s.kind, s.machine_id,
                           s.operator, s.start, s.end, qty) for s in keep)
    out.extend(added)
    out.sort(key=lambda s: (s.start, s.machine_id or "", s.order_key, s.op_seq))

    by_order = defaultdict(list)
    for s in out:
        by_order[s.order_key].append(s.end)
    completion = {k: max(v) for k, v in by_order.items()}
    for k, v in schedule.completion.items():          # never publish a LATER date
        if k in completion:
            completion[k] = min(completion[k], v)
        else:
            completion[k] = v
    return Schedule(tuple(out), completion)
