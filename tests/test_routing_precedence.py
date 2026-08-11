"""The plan must ALWAYS follow the routing in Item's Process Master (live 2026-08-09).

A director opened the shift-wise schedule and found CNC FIRST SIDE running on 11-08
while CNC SECOND SIDE, VMC FIRST SIDE, DEBURING and INSP — every step that eats its
output — had already run on 09-08 and 10-08. On a clean book the order is perfect;
the violation only appears once work is IN PROGRESS.

Root cause: `flow_scheduler._preplace_frozen` pinned every part-finished op onto its
machine at `machine_free[machine]`, grouped BY MACHINE, and never once consulted the
owning order's own predecessor (`ready_of`). So each in-progress step landed in its
machine's first free slot independently of the routing: a free CNC4 started step 3 on
Saturday while a busy CNC5 could not start step 2 until Monday. The main scheduling
loop has both a precedence gate and a piece-flow guard; the frozen path had neither.

Invariant, per (SO number, item code), for consecutive routing steps a then b:
    start(b) >  start(a)     b cannot begin before, or with, the step that feeds it
    end(b)   >= end(a)       b cannot finish before a finishes
Overlap (Rule 5) deliberately lets b START before a ENDS, and pacing deliberately
lets a fast b END exactly with a. Both stay legal; neither is flagged.
"""
import importlib
from datetime import date, datetime, timedelta

import pytest

from engine import book_store, freeze
from engine.models import Masters, Order, Process, Routing, ScheduleEntry
from engine.new_engine import routing_order_violations
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A, ITEM_B

T0 = datetime(2025, 3, 3, 8, 0)


def _masters_two_steps():
    return Masters(routings={"X": Routing(
        item_code="X", description="", customer="", rm_type="", moq=None, processes=[
        Process(seq=1, name="CNC FIRST SIDE", cycle_time=5.0, total_time=5.0,
                suggested_machine="CNC1", allotted_machine="CNC1"),
        Process(seq=2, name="CNC SECOND SIDE", cycle_time=5.0, total_time=5.0,
                suggested_machine="CNC2", allotted_machine="CNC2"),
    ])})


def _entry(seq, name, machine, start, end):
    return ScheduleEntry(batch_id="B1", item_code="X", process_seq=seq,
                         process_name=name, machine=machine, qty=10,
                         occupancy_min=(end - start).total_seconds() / 60.0,
                         start=start, end=end, so_refs=["SO1"])


# --------------------------------------------------------------------------- #
# The pure checker
# --------------------------------------------------------------------------- #
def test_a_later_step_running_before_the_step_that_feeds_it_is_flagged():
    m = _masters_two_steps()
    first = _entry(1, "CNC FIRST SIDE", "CNC1", T0 + timedelta(days=2), T0 + timedelta(days=3))
    second = _entry(2, "CNC SECOND SIDE", "CNC2", T0, T0 + timedelta(hours=6))
    hits = routing_order_violations([first, second], m)
    assert hits, "step 2 starting two days before step 1 must be reported"
    assert hits[0]["kind"] == "ROUTING_ORDER_VIOLATION"
    assert "CNC SECOND SIDE" in hits[0]["message"]
    assert "CNC FIRST SIDE" in hits[0]["message"]


def test_two_consecutive_steps_starting_at_the_same_instant_are_flagged():
    """The live shape once machines were free: five in-progress steps all pinned to
    the same minute. Deburring cannot begin the instant CNC begins."""
    m = _masters_two_steps()
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, T0 + timedelta(hours=8))
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0, T0 + timedelta(hours=9))
    assert routing_order_violations([a, b], m)


def test_overlap_is_legal_and_never_flagged():
    """Rule 5 lets a successor start while its predecessor is still cutting."""
    m = _masters_two_steps()
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, T0 + timedelta(hours=8))
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0 + timedelta(hours=2),
               T0 + timedelta(hours=10))
    assert routing_order_violations([a, b], m) == []


def test_a_paced_successor_ending_exactly_with_its_predecessor_is_legal():
    """Overlap pacing deliberately stretches a fast op's end to its predecessor's."""
    m = _masters_two_steps()
    end = T0 + timedelta(hours=8)
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, end)
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0 + timedelta(hours=2), end)
    assert routing_order_violations([a, b], m) == []


def test_a_successor_finishing_before_its_predecessor_is_flagged():
    m = _masters_two_steps()
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, T0 + timedelta(hours=8))
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0 + timedelta(hours=1),
               T0 + timedelta(hours=2))
    hits = routing_order_violations([a, b], m)
    assert any("finishes before" in h["message"] for h in hits)


# --------------------------------------------------------------------------- #
# The real bug: a part-finished order must still run in routing order
# --------------------------------------------------------------------------- #
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient   # noqa: E402


def _api(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")   # what production runs
    import api.main as m
    importlib.reload(m)
    return m


def _client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _plan_with_work_in_progress(m, c):
    """Orders part-finished at every step, with an applied plan on file — the state
    that pins in-progress ops. ITEM_B contends for the same CNCs, so the first step
    cannot start immediately while the later steps' machines sit free: exactly the
    shape that produced the live inversion."""
    book_store.save_masters_bytes(build_new_sample_bytes())
    # Enough orders to CONGEST the CNCs (step 1 of ITEM_A, step 2 of ITEM_B) while
    # VMC1 and MI1 — the machines for the LATER steps — sit comparatively free. That
    # gap is the whole bug: without the routing gate a free machine runs a later step
    # while the congested machine has not run the step feeding it. Verified to
    # discriminate: with the gate removed from `_preplace_frozen`, these tests fail.
    orders = []
    for i in range(8):
        orders.append(Order(f"SOA{i}", ITEM_A, ITEM_A, 120, date(2025, 3, 20 + i % 5)))
    for i in range(6):
        orders.append(Order(f"SOB{i}", ITEM_B, ITEM_B, 300, date(2025, 3, 20 + i % 5)))
    book_store.add_orders(orders)
    masters = m._current_masters()
    c.post("/run", json={"persist": False})
    book_store.save_last_applied_schedule(
        freeze.schedule_projection(m._PLAN_CACHE["artifacts"]["plan_run"].schedule))

    op = sorted({o.name for o in masters.operators})[0]
    for o in orders:
        so, item = o.so_no, o.item_code
        procs = masters.routings[item].processes
        # A descending ladder: every step part-done, none ahead of the step above it.
        for k, p in enumerate(procs[:-1]):
            r = c.post("/actuals", json={
                "so_no": so, "item_code": item, "item_name": item,
                "entry_date": "2025-03-10", "process": p.name,
                "qty_produced": max(20, 100 - 30 * k), "qty_rejected": 0,
                "operator": op, "shift": "1st shift", "machine": "",
                "downtime_min": 0, "remarks": ""})
            assert r.status_code == 200, r.text
    frozen_rows = m._compute_and_store_frozen()
    assert frozen_rows, "the setup must actually produce frozen in-progress ops"
    m._PLAN_CACHE.update(key=None, result=None)
    c.post("/run", json={"persist": False})
    return masters, m._PLAN_CACHE["artifacts"]["plan_run"].schedule


def test_an_order_already_in_progress_still_runs_in_routing_order():
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        c = _client(m)
        masters, schedule = _plan_with_work_in_progress(m, c)
        hits = routing_order_violations(schedule, masters)
        assert hits == [], "\n".join(h["message"] for h in hits)


def test_a_clean_book_runs_in_routing_order():
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        c = _client(m)
        book_store.save_masters_bytes(build_new_sample_bytes())
        book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 120, date(2025, 3, 20)),
                               Order("SO3", ITEM_B, ITEM_B, 300, date(2025, 3, 20))])
        masters = m._current_masters()
        c.post("/run", json={"persist": False})
        schedule = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
        assert routing_order_violations(schedule, masters) == []


def test_the_validation_report_surfaces_a_routing_order_violation():
    """Checked in production too, not only in tests — non-blocking, sitting beside
    the operator-qualification invariant."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        _client(m)
        book_store.save_masters_bytes(build_new_sample_bytes())
        book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
        masters = m._current_masters()
        bad = [
            ScheduleEntry(batch_id="B1", item_code=ITEM_A, process_seq=1,
                          process_name="CNC FIRST SIDE", machine="CNC1", qty=10,
                          occupancy_min=60, start=T0 + timedelta(days=2),
                          end=T0 + timedelta(days=3), so_refs=["SO1"]),
            ScheduleEntry(batch_id="B1", item_code=ITEM_A, process_seq=2,
                          process_name="VMC FIRST SIDE", machine="VMC1", qty=10,
                          occupancy_min=60, start=T0, end=T0 + timedelta(hours=6),
                          so_refs=["SO1"]),
        ]
        rep = m._report_for_book(masters, [], schedule=bad,
                                 config=m._load_plan_config())
        kinds = [r[0] for r in rep["rows"]]
        assert "ROUTING_ORDER_VIOLATION" in kinds


# --------------------------------------------------------------------------- #
# The optimizer must obey the same routing as the plan on screen
# --------------------------------------------------------------------------- #
def test_the_optimizer_replay_path_also_runs_in_routing_order():
    """`_all_lines_schedule` is how an optimized sequence is scored AND how Apply
    replays it. Hand it a deliberately hostile sequence (every order's rank
    reversed) on a book with work in progress: routing order must still hold, or
    the optimizer would be picking winners among physically impossible plans."""
    with pytest.MonkeyPatch.context() as mp:
        from engine import optimize_service
        m = _api(mp)
        c = _client(m)
        masters, _ = _plan_with_work_in_progress(m, c)

        setup = optimize_service.prepare_contest(
            book_store.load_active_orders(), book_store.load_actuals(), masters,
            m._resolve_config(m._load_plan_config()),
            absences=book_store.load_absences(),
            operator_table=book_store.load_operator_table(),
            frozen=book_store.load_frozen_ops())
        keys = [f"{l.so_no}\x1f{l.item_code}" for l in setup.target]
        hostile = {k: i for i, k in enumerate(reversed(keys))}

        for label, ranks in (("no ranks", None), ("reversed ranks", hostile)):
            schedule, _ = m._all_lines_schedule(setup, setup.masters, ranks)
            hits = routing_order_violations(schedule, masters)
            assert hits == [], f"{label}: " + "\n".join(h["message"] for h in hits)


def test_the_gantt_and_the_shift_wise_export_show_the_same_routing_order():
    """The two artifacts the directors actually open. Both are read from their own
    published output, not from the engine — a view that re-derives anything would
    show its own order, which is the whole class of bug being guarded here."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        c = _client(m)
        masters, _ = _plan_with_work_in_progress(m, c)
        cfg = m._load_plan_config().to_dict()
        cfg["apply_operator_logic"] = True          # what makes shift-wise exist
        m._PLAN_CACHE.update(key=None, result=None)
        run = c.post("/run", json={"persist": True, "config": cfg}).json()

        pos = {}
        for item, r in masters.routings.items():
            for i, p in enumerate(r.processes):
                pos[(item, p.name.strip().upper())] = i

        def violations(spans):
            out = []
            for key, by in spans.items():
                steps = sorted(by.items())
                for (_ia, (sa, ea)), (_ib, (sb, eb)) in zip(steps, steps[1:]):
                    if sb < sa or (sb == sa and ea > sa) or eb < ea:
                        out.append(f"{key}: step order broken at {sb}")
            return out

        gantt = {}
        for row in run["gantt"]["rows"]:
            item = row["item_code"]
            for so in [s.strip() for s in (row["so_no"] or "").split(",") if s.strip()]:
                for bar in row["bars"]:
                    i = pos.get((item, bar["process"].strip().upper()))
                    if i is None:
                        continue
                    s = datetime.strptime(bar["start"], "%d-%m-%Y %H:%M")
                    e = datetime.strptime(bar["end"], "%d-%m-%Y %H:%M")
                    cur = gantt.setdefault((so, item), {}).get(i)
                    gantt[(so, item)][i] = ((min(cur[0], s), max(cur[1], e))
                                            if cur else (s, e))
        assert gantt, "the Gantt must have bars to check"
        assert violations(gantt) == []

        sw = run["trace"]["rule6"].get("shiftwise")
        assert sw and sw["rows"], "the shift-wise export must exist with operator logic on"
        col = {n: i for i, n in enumerate(sw["columns"])}
        rows = {}
        for r in sw["rows"]:
            item, proc = str(r[col["Item Code"]]), str(r[col["Process"]])
            pname = proc.split(".", 1)[1].strip() if proc[:2].strip().rstrip(".").isdigit() else proc
            i = pos.get((item, pname.strip().upper()))
            if i is None:
                continue
            year = str(r[col["Date"]])[-4:]
            s = datetime.strptime(f"{r[col['Start']]}-{year}", "%d-%m %H:%M-%Y")
            e = datetime.strptime(f"{r[col['End']]}-{year}", "%d-%m %H:%M-%Y")
            for so in [x.strip() for x in str(r[col["SO No"]]).split(",") if x.strip()]:
                cur = rows.setdefault((so, item), {}).get(i)
                rows[(so, item)][i] = ((min(cur[0], s), max(cur[1], e))
                                       if cur else (s, e))
        assert rows, "the shift-wise export must have parseable rows"
        assert violations(rows) == []


# --------------------------------------------------------------------------- #
# Engine level: the frozen pre-placement itself, on IDLE machines
# --------------------------------------------------------------------------- #
def _frozen_ctx():
    """One order with every real step frozen, and every machine otherwise idle —
    the geometry the routing gate exists for. Nothing else competes, so if the gate
    is missing each step is laid at its own machine's free time, i.e. all at once."""
    import io
    from engine.config import Config as _Config
    from engine.new_engine import _orders_from_batches, _plan_config
    from engine.rules import rule1_consolidate
    from engine import loaders as _loaders
    from ppc_engine.loaders import load_all as _new_load

    conf = _Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                   apply_operator_logic=True)
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = _new_load(io.BytesIO(wb)).masters
    so_lines, _ = _loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, conf)
    orders, _ = _orders_from_batches(batches, nm)
    return orders, [o.key for o in orders], nm, _plan_config(conf)


def _starts_by_seq(sched, key):
    out = {}
    for s in sched.segments:
        if s.order_key != key:
            continue
        cur = out.get(s.op_seq)
        out[s.op_seq] = (min(cur[0], s.start), max(cur[1], s.end)) if cur else (s.start, s.end)
    return out


def _freeze_all_real_ops(order, nm, cfg, prev_starts, qtys=None):
    """Freeze every real step. `qtys` defaults to a SHORT step feeding a LONG one —
    the case only the routing gate catches. When the successor is longer, its end
    already clears its predecessor's, so the piece-flow guard is satisfied and would
    happily start it in the same minute; only `ready_of` stops that. (Verified by
    mutation: drop the gate from `_preplace_frozen` and this fails.)"""
    from ppc_engine.scheduler import FrozenOp
    routing = nm.routings[order.item_code]
    real = [op for op in routing.operations if op.machine_options and op.cycle_min > 0]
    qtys = qtys or ([5] * (len(real) - 1) + [400])
    return real, [FrozenOp(order_key=order.key, op_seq=op.seq,
                           machine_id=op.machine_options[0], operator="",
                           remaining_qty=qtys[i], prev_start=prev_starts[i])
                  for i, op in enumerate(real)]


def test_frozen_steps_of_one_order_never_run_together_on_idle_machines():
    """Every machine free, every step in progress. Each step still has to wait for
    the step that feeds it — laying them all at their own machine's free time would
    start the whole routing in the same minute."""
    from datetime import timedelta as _td
    orders, seq, nm, cfg = _frozen_ctx()
    o0 = orders[0]
    real, frozen = _freeze_all_real_ops(
        o0, nm, cfg, [cfg.plan_start + _td(hours=i) for i in range(9)])
    assert len(real) >= 3, "need a routing with at least three real steps"

    from ppc_engine.scheduler import decode as _decode
    starts = _starts_by_seq(_decode(orders, seq, nm, cfg, frozen=frozen), o0.key)
    ordered = [starts[op.seq] for op in real if op.seq in starts]
    assert len(ordered) == len(real), "a frozen step went missing"
    for (sa, _ea), (sb, _eb) in zip(ordered, ordered[1:]):
        assert sb > sa, (
            "a frozen step starts at or before the step feeding it: "
            + " | ".join(f"{op.name}@{starts[op.seq][0]:%d-%m %H:%M}" for op in real))


def test_frozen_steps_follow_the_routing_even_when_the_last_plan_disagrees():
    """The previous plan's order is a preference; the routing is physics. Hand the
    pre-placement a previous plan that ran the LAST step first and the routing must
    still win."""
    from datetime import timedelta as _td
    orders, seq, nm, cfg = _frozen_ctx()
    o0 = orders[0]
    real, frozen = _freeze_all_real_ops(
        o0, nm, cfg, [cfg.plan_start + _td(hours=9 - i) for i in range(9)])

    from ppc_engine.scheduler import decode as _decode
    starts = _starts_by_seq(_decode(orders, seq, nm, cfg, frozen=frozen), o0.key)
    ordered = [starts[op.seq] for op in real if op.seq in starts]
    for (sa, _ea), (sb, _eb) in zip(ordered, ordered[1:]):
        assert sb > sa, (
            "reversed previous-plan order beat the routing: "
            + " | ".join(f"{op.name}@{starts[op.seq][0]:%d-%m %H:%M}" for op in real))


def test_a_resumed_op_does_not_delay_its_successor_by_a_setup_it_never_paid():
    """`_preplace_frozen` charges NO setup on resume ("dur = remaining_qty * cycle"),
    but the routing gate added `config.setup_min` to the successor's release anyway —
    90 minutes of CNC setup nobody spends, on every in-progress machining op. Found
    while attributing a live late-days rise (2026-08-09).

    Tested by CHANGING the setup time: a resumed op pays no setup, so its successor
    must not move at all. (Only this one order is scheduled, so nothing else can move
    it either.)"""
    from dataclasses import replace as _replace
    from ppc_engine.scheduler import FrozenOp, decode as _decode
    orders, _seq, nm, cfg = _frozen_ctx()
    o0 = orders[0]
    real = [op for op in nm.routings[o0.item_code].operations
            if op.machine_options and op.cycle_min > 0]
    first, second = real[0], real[1]
    frozen = [FrozenOp(order_key=o0.key, op_seq=first.seq,
                       machine_id=first.machine_options[0], operator="",
                       remaining_qty=100, prev_start=cfg.plan_start)]

    starts = {}
    for setup in (0.0, 90.0, 240.0):
        c = _replace(cfg, setup_min=setup)
        sched = _decode([o0], [o0.key], nm, c, frozen=frozen)
        starts[setup] = _starts_by_seq(sched, o0.key)[second.seq][0]
    assert starts[0.0] == starts[90.0] == starts[240.0], (
        "the successor of a RESUMED op moved when setup_min changed, so a setup that "
        f"is never charged is being counted against it: {starts}")
