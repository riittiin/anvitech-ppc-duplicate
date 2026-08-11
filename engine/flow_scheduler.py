"""Flow scheduler — the productized Sched2 (owner mandate 2026-07-19).

Same contract as ``rule6_allocate.run``: pure
``run(prioritized_batches, config, masters, reserved=None) -> list[ScheduleEntry]``.

The owner's three basics are the only hard rules:
  1. every working minute has the machine AND a qualified operator,
  2. operators work only within their own shift (Thursdays/holidays off),
  3. process order per the routing is respected piece-wise.

Deliberately different from classic Rule 6 (measured worth ~27 days on the
real book — see docs/superpowers/specs/2026-07-19-flow-scheduler-design.md):
  * NO resource-holding: a machine/operator is occupied only while cutting
    pieces that already exist; a starved step releases the machine.
  * chunked piece-flow: qty moves between steps in ``qty / config.flow_chunks``
    chunks; successor chunks may run concurrently on alternative machines.
  * setup (90 min, CNC/VMC only) per machine RE-ENGAGEMENT of a different op;
    consecutive chunks of the same op pay once.

Feedback ("continue from reality"): ``batch.process_qty`` — pieces already
punched through step k-1 are initial WIP for step k; a step with remaining 0
schedules nothing. ``reserved={operator|machine: [(start, end), ...]}`` blocks
absences. Guard exhaustion raises RuleError — fail loud, never under-schedule.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

from .models import ScheduleEntry
from .loaders import normalize_process_name, normalize_resource_id
from .rules import rule6_allocate as r6


def _windows_for_day(d: date, hours_per_day: float, calendar, config):
    """Machine working windows for one calendar day (list of (start, end))."""
    if not calendar.is_working_day(d):
        return []
    base = datetime.combine(d, dtime(0, 0))
    fs = getattr(config, "first_shift_start_hour", 8)
    fe = getattr(config, "first_shift_end_hour", 19)
    se = getattr(config, "second_shift_end_hour", 5)
    thr = getattr(config, "two_shift_threshold_hours", 12.0)
    if hours_per_day >= thr:
        return [(base + timedelta(hours=fs), base + timedelta(hours=fe)),
                (base + timedelta(hours=fe), base + timedelta(hours=24 + se))]
    ms = getattr(config, "manual_start_hour", 9)
    me = getattr(config, "manual_end_hour", 18)
    return [(base + timedelta(hours=ms), base + timedelta(hours=me))]


def _op_window_for_day(d: date, kind: str, calendar, config):
    if not calendar.is_working_day(d):
        return []
    base = datetime.combine(d, dtime(0, 0))
    fs = getattr(config, "first_shift_start_hour", 8)
    fe = getattr(config, "first_shift_end_hour", 19)
    se = getattr(config, "second_shift_end_hour", 5)
    if kind == "first":
        return [(base + timedelta(hours=fs), base + timedelta(hours=fe))]
    if kind == "second":
        return [(base + timedelta(hours=fe), base + timedelta(hours=24 + se))]
    ms = getattr(config, "manual_start_hour", 9)
    me = getattr(config, "manual_end_hour", 18)
    return [(base + timedelta(hours=ms), base + timedelta(hours=me))]


def _blocked_until(reservations, t):
    """If t falls inside a reservation, its end; else None."""
    for s, e in reservations:
        if s <= t < e:
            return e
    return None


def _next_block_start(reservations, s, e):
    """Earliest reservation start inside (s, e), or None."""
    best = None
    for rs, _re in reservations:
        if s < rs < e and (best is None or rs < best):
            best = rs
    return best


# Version token for the FLOW scheduler's semantics, folded into
# api._inputs_signature next to rule6's (bump on any behaviour change here).
FLOW_FINGERPRINT = "flow-v2-setup-aware"


class _Flow:
    def __init__(self, batches, config, masters, reserved=None):
        self.batches = batches
        self.config = config
        self.masters = masters
        self.reserved = reserved or {}
        self.calendar = masters.calendar
        self.t0 = datetime.combine(config.plan_start_date,
                                   dtime(getattr(config, "first_shift_start_hour", 8), 0))
        self.n_chunks = max(1, int(getattr(config, "flow_chunks", 4) or 4))
        self.setup_min = float(config.setup_time_min)
        self.setup_aware = bool(getattr(config, "flow_setup_aware", True))
        self.op_logic = bool(getattr(config, "apply_operator_logic", False))

        self.mac_hours = {}
        for mid, mac in masters.machines.items():
            self.mac_hours[mid] = getattr(mac, "available_hrs_per_day", None) or 19.5

        self.op_kind, self.op_quals = {}, {}
        for op in (masters.operators if self.op_logic else []):
            s = (getattr(op, "shift", "") or "").lower()
            self.op_kind[op.name] = ("first" if "first" in s else
                                     "second" if "second" in s else "manual")
            self.op_quals[op.name] = set(getattr(op, "machines", []) or [])
        self.op_rank = r6._operator_flexibility(masters) if self.op_logic else {}

        self.machine_free: dict[str, datetime] = {}
        self.machine_last_op: dict[str, tuple] = {}
        self.op_free: dict[str, datetime] = {}
        self.entries: list[ScheduleEntry] = []

    # ---------------- resources ----------------
    def _mac_next_window(self, mid, t):
        h = self.mac_hours.get(mid, 19.5)
        m_res = self.reserved.get(mid, [])
        d = t.date() - timedelta(days=1)
        for _ in range(1500):
            for ws, we in _windows_for_day(d, h, self.calendar, self.config):
                if we > t:
                    s = max(ws, t)
                    cov = _blocked_until(m_res, s)
                    if cov is not None:
                        if cov < we:
                            s = cov
                        else:
                            continue
                    cut = _next_block_start(m_res, s, we)
                    return s, (cut if cut is not None else we)
            d += timedelta(days=1)
        from .pipeline import RuleError
        raise RuleError("rule6", mid, "no working window found in 4 years")

    def _qualified(self, mid):
        if not self.op_logic:
            return []
        mac = self.masters.machines.get(mid)
        keys = {mid}
        if mac is not None:
            keys.add(normalize_resource_id(getattr(mac, "machine_type", "") or ""))
        return [n for n, q in self.op_quals.items() if q & keys]

    def _op_cover_end(self, name, t):
        for delta in (-1, 0):
            d = t.date() + timedelta(days=delta)
            for ws, we in _op_window_for_day(d, self.op_kind[name], self.calendar, self.config):
                if ws <= t < we:
                    return we
        return None

    def _op_next_cover(self, name, t):
        d = t.date() - timedelta(days=1)
        for _ in range(1500):
            for ws, we in _op_window_for_day(d, self.op_kind[name], self.calendar, self.config):
                if we > t:
                    return max(ws, t)
            d += timedelta(days=1)
        from .pipeline import RuleError
        raise RuleError("rule6", name, "operator never on duty in 4 years")

    def _lay(self, mid, earliest, work_min, tag):
        """Book work_min on mid with a qualified on-shift operator per segment
        (scarce-first among free), honoring operator reservations. Returns
        (start, end, segments); resource state committed separately."""
        names = self._qualified(mid)
        cursor = max(earliest, self.machine_free.get(mid, self.t0))
        remaining = float(work_min)
        segments = []
        start = None
        guard = 0
        while remaining > 1e-9:
            guard += 1
            if guard > 5000:
                from .pipeline import RuleError
                raise RuleError("rule6", tag, f"segment guard exhausted on {mid}")
            cursor, wend = self._mac_next_window(mid, cursor)
            if not names:
                seg_end = min(wend, cursor + timedelta(minutes=remaining))
                got = (seg_end - cursor).total_seconds() / 60.0
                if got <= 1e-9:
                    cursor = cursor + timedelta(minutes=1)
                    continue
                segments.append((cursor, seg_end, ""))
                start = start or cursor
                remaining -= got
                cursor = seg_end
                continue
            free = []
            for n in names:
                cov_end = self._op_cover_end(n, cursor)
                if cov_end is None:
                    continue
                if self.op_free.get(n, self.t0) > cursor:
                    continue
                if _blocked_until(self.reserved.get(n, []), cursor) is not None:
                    continue
                free.append((n, cov_end))
            if not free:
                nxt = None
                for n in names:
                    t = max(self.op_free.get(n, self.t0), cursor)
                    cov = _blocked_until(self.reserved.get(n, []), t)
                    if cov is not None:
                        t = cov
                    t = self._op_next_cover(n, t)
                    if nxt is None or t < nxt:
                        nxt = t
                cursor = max(nxt, cursor + timedelta(minutes=1))
                continue
            free.sort(key=lambda nc: (self.op_rank.get(nc[0], 99),
                                      self.op_free.get(nc[0], self.t0)))
            name, cov_end = free[0]
            seg_end = min(wend, cov_end, cursor + timedelta(minutes=remaining))
            cut = _next_block_start(self.reserved.get(name, []), cursor, seg_end)
            if cut is not None:
                seg_end = cut
            got = (seg_end - cursor).total_seconds() / 60.0
            if got <= 1e-9:
                cursor = cursor + timedelta(minutes=1)
                continue
            segments.append((cursor, seg_end, name))
            start = start or cursor
            remaining -= got
            cursor = seg_end
        return start, cursor, segments

    def _commit(self, mid, segments, op_key):
        end = segments[-1][1]
        self.machine_free[mid] = end
        self.machine_last_op[mid] = op_key
        for ss, se, name in segments:
            if name:
                self.op_free[name] = max(self.op_free.get(name, self.t0), se)

    # ---------------- build ----------------
    def build(self):
        cfg, masters = self.config, self.masters
        jobs = []
        for prio, b in enumerate(self.batches):
            routing = masters.routings.get(b.item_code)
            if routing is None:
                from .pipeline import RuleError
                raise RuleError("rule6", b.batch_id, f"no routing for {b.item_code}")
            qty = b.qty
            pq = getattr(b, "process_qty", None)

            def rem_of(p):
                if pq is None:
                    return qty
                return max(min(pq.get(normalize_process_name(p.name), qty), qty), 0.0)

            steps = []
            for p in routing.processes:
                if r6._is_offmachine(p):
                    steps.append({"kind": "milestone", "p": p})
                    continue
                if r6._is_os(p):
                    rem = rem_of(p)
                    steps.append({"kind": "os", "p": p,
                                  "done_at": self.t0 if rem <= 0 else None})
                    continue
                cands = r6._resolve_candidates(p, cfg)
                cands = [c for c in cands if c != "OS"]
                if not cands or not p.cycle_time:
                    steps.append({"kind": "milestone", "p": p})
                    continue
                rem = rem_of(p)
                steps.append({"kind": "real", "p": p, "cands": cands,
                              "cyc": float(p.cycle_time), "rem": rem,
                              "sched_qty": 0.0,
                              # pieces that already cleared THIS step before the
                              # plan (the feedback loop's initial state):
                              "pre_done": qty - rem,
                              "done_curve": []})
            jobs.append({"b": b, "prio": prio, "steps": steps,
                         "chunk": max(qty / self.n_chunks, 1.0)})

        def pred_done_at(job, si, t):
            """Pieces available FROM the predecessor side to step si by time t
            (pre-plan progress + scheduled arrivals)."""
            if si == 0:
                return job["b"].qty
            prev = job["steps"][si - 1]
            if prev["kind"] == "milestone":
                return pred_done_at(job, si - 1, t)
            if prev["kind"] == "os":
                return job["b"].qty if (prev["done_at"] and prev["done_at"] <= t) else 0.0
            return prev["pre_done"] + sum(q for te, q in prev["done_curve"] if te <= t)

        def pred_next_arrival(job, si, after):
            if si == 0:
                return None
            prev = job["steps"][si - 1]
            if prev["kind"] == "milestone":
                return pred_next_arrival(job, si - 1, after)
            if prev["kind"] == "os":
                da = prev["done_at"]
                return da if (da and da > after) else None
            best = None
            for te, q in prev["done_curve"]:
                if te > after and (best is None or te < best):
                    best = te
            return best

        guard = 0
        while True:
            guard += 1
            if guard > 500000:
                from .pipeline import RuleError
                raise RuleError("rule6", "-", "main scheduling loop guard exhausted")
            best = None
            best_key = None      # effective-start comparison key (setup-aware)
            for j in jobs:
                qty = j["b"].qty
                for si, st in enumerate(j["steps"]):
                    if st["kind"] == "milestone":
                        continue
                    if st["kind"] == "os":
                        if st["done_at"] is not None:
                            continue
                        t = self.t0
                        while pred_done_at(j, si, t) < qty - 1e-6:
                            t = pred_next_arrival(j, si, t)
                            if t is None:
                                break
                        if t is None:
                            continue
                        key = (t, 0, j["prio"], si)   # OS never needs a setup
                        if best_key is None or key < best_key:
                            best_key = key
                            best = (t, j["prio"], si, j, st, None, 0.0)
                        continue
                    if st["sched_qty"] >= st["rem"] - 1e-6:
                        continue
                    remaining = st["rem"] - st["sched_qty"]
                    # already-consumed pieces at this step = pre_done + sched_qty
                    t = self.t0
                    avail = pred_done_at(j, si, t) - st["pre_done"] - st["sched_qty"]
                    need = min(j["chunk"], remaining)
                    while avail < need - 1e-6:
                        t = pred_next_arrival(j, si, t)
                        if t is None:
                            break
                        avail = pred_done_at(j, si, t) - st["pre_done"] - st["sched_qty"]
                    if t is None:
                        continue
                    run_qty = min(avail, remaining, j["chunk"])
                    op_key = (j["b"].batch_id, st["p"].seq)
                    for mid in st["cands"]:
                        est = max(t, self.machine_free.get(mid, self.t0))
                        try:
                            est, _we = self._mac_next_window(mid, est)
                        except Exception:
                            continue
                        # Setup-aware TIE-BREAK: keep earliest-start (makespan) as
                        # the primary key, but when two candidates could start at the
                        # SAME instant, prefer the one that does NOT need a fresh
                        # CNC/VMC setup (machine already running this op's chunks).
                        # Chunk boundaries create many exact ties on a just-freed
                        # machine, so this batches a job's consecutive chunks and
                        # cuts re-setups without ever delaying an earlier start.
                        # Off -> byte-identical to the setup-blind earliest-start rule.
                        needs_setup = int(
                            self.setup_aware
                            and r6._is_setup_machine(mid, self.masters)
                            and self.machine_last_op.get(mid) != op_key)
                        key = (est, needs_setup, j["prio"], si)
                        if best_key is None or key < best_key:
                            best_key = key
                            best = (est, j["prio"], si, j, st, mid, run_qty)
            if best is None:
                break
            est, _prio, si, j, st, mid, run_qty = best
            b = j["b"]
            if st["kind"] == "os":
                dur = float(st["p"].cycle_time or 0.0)
                edt = est + timedelta(minutes=dur)
                st["done_at"] = edt
                self.entries.append(ScheduleEntry(
                    batch_id=b.batch_id, item_code=b.item_code,
                    process_seq=st["p"].seq, process_name=st["p"].name,
                    machine="OS / Outsourced", qty=b.qty, occupancy_min=dur,
                    start=est, end=edt, so_refs=list(b.source_so_refs or []),
                    op_segments=[],
                    notes="OS vendor turnaround: flat block, continuous 24x7, "
                          "no in-house machine/operator"))
                continue
            op_key = (b.batch_id, st["p"].seq)
            setup = (self.setup_min
                     if (r6._is_setup_machine(mid, masters)
                         and self.machine_last_op.get(mid) != op_key) else 0.0)
            work = st["cyc"] * run_qty + setup
            sdt, edt, segs = self._lay(mid, est, work, f"{b.batch_id}/{st['p'].seq}")
            self._commit(mid, segs, op_key)
            st["sched_qty"] += run_qty
            st["done_curve"].append((edt, run_qty))
            self.entries.append(ScheduleEntry(
                batch_id=b.batch_id, item_code=b.item_code,
                process_seq=st["p"].seq, process_name=st["p"].name,
                machine=mid, qty=run_qty, occupancy_min=work,
                start=sdt, end=edt, so_refs=list(b.source_so_refs or []),
                op_segments=segs, operator=segs[0][2] if segs else "",
                notes=("chunk flow" if run_qty < b.qty - 1e-9 else "")))

        # milestones (DISPATCH and other off-machine steps) at batch completion
        for j in jobs:
            b = j["b"]
            ends = [e.end for e in self.entries if e.batch_id == b.batch_id]
            at = max(ends) if ends else self.t0
            for st in j["steps"]:
                if st["kind"] != "milestone":
                    continue
                p = st["p"]
                lane = r6._offmachine_lane(p)
                self.entries.append(ScheduleEntry(
                    batch_id=b.batch_id, item_code=b.item_code,
                    process_seq=p.seq, process_name=p.name,
                    machine=lane, qty=b.qty,
                    occupancy_min=0.0, start=at, end=at,
                    so_refs=list(b.source_so_refs or []), op_segments=[],
                    notes="off-machine step (OS / outsourcing / dispatch): no "
                          "in-house machine or time; shown as a milestone"))
        self.entries.sort(key=lambda e: (e.start, e.batch_id, e.process_seq))
        return self.entries


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, frozen=None):
    """Flow-schedule the prioritized batches. Same signature as
    ``rule6_allocate.run``; ``machine_lost_min`` is accepted for signature
    parity and ignored (the feedback loop is quantity-only). ``frozen`` is
    accepted but ignored (flow engine ignores frozen)."""
    from .config import Config
    config = config or Config()
    if masters is None:
        from .pipeline import RuleError
        raise RuleError("rule6", "-", "masters are required to allocate")
    sched = _Flow(list(batches), config, masters, reserved=reserved).build()
    if notes is not None:
        notes.append(
            f"flow scheduler: chunked piece-flow (chunks={max(1, int(getattr(config, 'flow_chunks', 4) or 4))}), "
            f"no resource-holding, setup per re-engagement, scarce-first crewing")
    return sched
