"""Order-book pure logic: merge, status derivation, active lines, persistence.

Identity note: an order is uniquely the pair **(SO number, item code)** — one SO
number can carry several item lines, each tracked as its own order. Every keyed
structure here (good-by-order, orders-with-actuals, per-process progress, the
storage hash) is keyed by that pair, never by SO number alone.
"""
from datetime import date

from engine.models import SOLine, Order, Actual, Masters, Routing, Process, WorkCalendar
from engine import orderbook, book_store


def _so(no, item, qty, d):
    return SOLine(so_no=no, item_code=item, item_name=item, qty=qty, delivery_date=d)


def _order(no, item, qty, d, completed=False):
    return Order(so_no=no, item_code=item, item_name=item, ordered_qty=qty,
                 delivery_date=d, completed=completed)


D = date(2025, 8, 1)


def test_merge_adds_unseen_and_flags_known():
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    completed = {("SO9", "Z"): _order("SO9", "Z", 5, D, completed=True)}
    lines = [_so("SO1", "A", 10, D), _so("SO2", "B", 20, D), _so("SO9", "Z", 5, D)]

    new, updated, flags = orderbook.merge_upload(lines, active, completed)
    assert updated == []
    assert [(o.so_no, o.item_code) for o in new] == [("SO2", "B")]   # only the unseen one
    reasons = {(f["so_no"], f["item_code"]): f["reason"] for f in flags}
    assert "duplicate" in reasons[("SO1", "A")]
    assert "already completed" in reasons[("SO9", "Z")]


def test_same_so_different_item_are_two_distinct_orders():
    """The whole point of the redesign: SO No is NOT unique. Two lines that share
    an SO number but carry different item codes are two separate orders."""
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}     # SO1/A already in the book
    # Upload SO1 again — but item B. Same SO#, different item => a brand-new order.
    new, updated, flags = orderbook.merge_upload([_so("SO1", "B", 20, D)], active, {})
    assert [(o.so_no, o.item_code) for o in new] == [("SO1", "B")]   # added, not flagged
    assert updated == []
    assert flags == []
    # And SO1/A on the SAME upload is still the duplicate it always was.
    new2, updated2, flags2 = orderbook.merge_upload([_so("SO1", "A", 10, D)], active, {})
    assert new2 == [] and updated2 == [] and "duplicate" in flags2[0]["reason"]


def test_merge_does_not_update_quantity_only_the_date():
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    new, updated, flags = orderbook.merge_upload([_so("SO1", "A", 99, D)], active, {})
    assert new == [] and updated == []
    assert flags[0]["reason"] == "changed: only the delivery date can be updated by re-import"
    assert active[("SO1", "A")].ordered_qty == 10


def test_merge_dedupes_within_upload_by_pair():
    # Same (SO, item) twice in one file => second flagged; same SO diff item => both kept.
    lines = [_so("SO1", "A", 10, D), _so("SO1", "A", 10, D), _so("SO1", "B", 5, D)]
    new, updated, flags = orderbook.merge_upload(lines, {}, {})
    assert updated == []
    assert [(o.so_no, o.item_code) for o in new] == [("SO1", "A"), ("SO1", "B")]
    assert any("duplicate" in f["reason"] for f in flags)


D2 = date(2025, 9, 15)


def test_merge_updates_delivery_date_and_nothing_else():
    """The director's use case: edit SO Delivery Date in Excel, re-import."""
    ex = Order(so_no="SO1", item_code="A", item_name="A", ordered_qty=10,
               delivery_date=D, first_seen="2025-08-01", commitment="committed",
               promised_date=D, committed_at="2025-08-01T10:00:00")
    active = {("SO1", "A"): ex}

    new, updated, flags = orderbook.merge_upload([_so("SO1", "A", 10, D2)], active, {})

    assert new == []
    assert len(updated) == 1
    u = updated[0]
    assert u.delivery_date == D2                 # the one field that moved
    # Everything else survives untouched.
    assert (u.so_no, u.item_code, u.item_name) == ("SO1", "A", "A")
    assert u.ordered_qty == 10
    assert u.commitment == "committed"
    assert u.promised_date == D                  # the PROMISE does not follow the SO date
    assert u.committed_at == "2025-08-01T10:00:00"
    assert u.first_seen == "2025-08-01"
    assert u.completed is False
    # The input dict is never mutated (merge_upload is pure).
    assert active[("SO1", "A")].delivery_date == D
    # The report names both dates, day-first.
    assert flags[0]["reason"] == "delivery date updated: 01-08-2025 → 15-09-2025"


def test_merge_updates_date_but_ignores_a_qty_change_on_the_same_row():
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    new, updated, flags = orderbook.merge_upload([_so("SO1", "A", 99, D2)], active, {})
    assert len(updated) == 1
    assert updated[0].delivery_date == D2
    assert updated[0].ordered_qty == 10          # qty change ignored, as specified


def test_merge_blank_uploaded_date_never_wipes_the_existing_one():
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    new, updated, flags = orderbook.merge_upload([_so("SO1", "A", 10, None)], active, {})
    assert new == [] and updated == []
    assert flags[0]["reason"] == "delivery date missing or unreadable, kept the old date"
    assert active[("SO1", "A")].delivery_date == D


def test_merge_unchanged_row_is_still_a_plain_duplicate():
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    new, updated, flags = orderbook.merge_upload([_so("SO1", "A", 10, D)], active, {})
    assert new == [] and updated == []
    assert flags[0]["reason"] == "duplicate: already in the book"


def test_merge_never_updates_a_completed_order():
    completed = {("SO9", "Z"): _order("SO9", "Z", 5, D, completed=True)}
    new, updated, flags = orderbook.merge_upload([_so("SO9", "Z", 5, D2)], {}, completed)
    assert new == [] and updated == []
    assert "already completed" in flags[0]["reason"]


def test_merge_updates_a_repeated_key_only_once_per_upload():
    """Same (SO#, item) twice in one file with two different dates: the first wins,
    the second is an intra-upload duplicate — never two updates for one order."""
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    lines = [_so("SO1", "A", 10, D2), _so("SO1", "A", 10, date(2025, 10, 1))]
    new, updated, flags = orderbook.merge_upload(lines, active, {})
    assert len(updated) == 1
    assert updated[0].delivery_date == D2
    assert "duplicate (SO#, item) within this upload" in flags[1]["reason"]


def test_status_derivation():
    o = _order("SO1", "A", 10, D)
    assert orderbook.derive_status(o, set()) == orderbook.PENDING
    assert orderbook.derive_status(o, {("SO1", "A")}) == orderbook.RUNNING   # has an actual
    # A different item on the same SO has NOT started just because SO1/A did.
    assert orderbook.derive_status(_order("SO1", "B", 3, D), {("SO1", "A")}) == orderbook.PENDING
    o.completed = True
    assert orderbook.derive_status(o, {("SO1", "A")}) == orderbook.COMPLETE


def test_active_so_lines_use_remaining_and_skip_done():
    active = {
        ("SO1", "A"): _order("SO1", "A", 10, D),      # produced 4 -> remaining 6
        ("SO2", "B"): _order("SO2", "B", 5, D),       # produced 5 -> remaining 0 -> skipped
        ("SO3", "C"): _order("SO3", "C", 8, D, completed=True),   # completed -> skipped
    }
    actuals = [
        Actual("SO1", "A", D, qty_produced=4),
        Actual("SO2", "B", D, qty_produced=5),
    ]
    lines = orderbook.active_so_lines(active, actuals)
    by = {(l.so_no, l.item_code): l.qty for l in lines}
    assert by == {("SO1", "A"): 6}                           # only SO1/A, at remaining 6


def test_active_so_lines_isolate_progress_per_item_on_same_so():
    """Punching SO1/A must not shrink SO1/B (same SO#, different item)."""
    active = {("SO1", "A"): _order("SO1", "A", 10, D),
              ("SO1", "B"): _order("SO1", "B", 10, D)}
    actuals = [Actual("SO1", "A", D, qty_produced=4)]        # only A produced
    by = {(l.so_no, l.item_code): l.qty for l in orderbook.active_so_lines(active, actuals)}
    assert by == {("SO1", "A"): 6, ("SO1", "B"): 10}         # B untouched


def test_delete_orders_removes_order_and_its_actuals():
    book_store.add_orders([_order("SO1", "A", 10, D), _order("SO2", "B", 5, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))
    book_store.append_actual(Actual("SO2", "B", D, qty_produced=2))

    book_store.delete_orders([("SO1", "A")])
    assert ("SO1", "A") not in book_store.load_active_orders()
    assert ("SO2", "B") in book_store.load_active_orders()
    remaining = book_store.load_actuals()
    assert [a.so_no for a in remaining] == ["SO2"]   # SO1/A's actual purged


def test_delete_one_item_line_keeps_the_other_on_same_so():
    book_store.add_orders([_order("SO1", "A", 10, D), _order("SO1", "B", 7, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))
    book_store.append_actual(Actual("SO1", "B", D, qty_produced=3))

    book_store.delete_orders([("SO1", "A")])                 # delete only the A line
    active = book_store.load_active_orders()
    assert ("SO1", "A") not in active and ("SO1", "B") in active
    assert [(a.so_no, a.item_code) for a in book_store.load_actuals()] == [("SO1", "B")]


def test_delete_all_wipes_orders_and_actuals():
    book_store.add_orders([_order("SO1", "A", 10, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))
    book_store.delete_all()
    assert book_store.load_active_orders() == {}
    assert book_store.load_completed_orders() == {}
    assert book_store.load_actuals() == []


def test_persistence_round_trip_and_complete():
    book_store.add_orders([_order("SO1", "A", 10, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))

    active = book_store.load_active_orders()
    assert active[("SO1", "A")].ordered_qty == 10
    assert len(book_store.load_actuals()) == 1

    assert book_store.complete_order("SO1", "A") is True
    assert ("SO1", "A") not in book_store.load_active_orders()
    assert book_store.load_completed_orders()[("SO1", "A")].completed is True


def test_complete_and_uncomplete_target_one_item_line():
    book_store.add_orders([_order("SO1", "A", 10, D), _order("SO1", "B", 7, D)])
    assert book_store.complete_order("SO1", "A") is True
    active = book_store.load_active_orders()
    assert ("SO1", "A") not in active and ("SO1", "B") in active   # only A archived
    assert book_store.uncomplete_order("SO1", "A") is True
    assert ("SO1", "A") in book_store.load_active_orders()


# --------------------------------------------------------------------------- #
# Shared routing / masters / actual helpers (finished-gate + per-process tests)
# --------------------------------------------------------------------------- #
def _routing(item, procs):
    """procs = list of (process_name, suggested_machine)."""
    return Routing(
        item_code=item, description="", customer="", rm_type="", moq=None,
        processes=[Process(seq=i + 1, name=name, cycle_time=1, total_time=None,
                           suggested_machine=sm, allotted_machine=None)
                   for i, (name, sm) in enumerate(procs)],
    )


def _masters(*routings):
    return Masters(routings={r.item_code: r for r in routings})


def _actual(item, process, **kw):
    return Actual(so_no="SO1", item_code=item, entry_date=D, process=process, **kw)


# --------------------------------------------------------------------------- #
# Finished-goods gate: WIP (intermediate-process good) must NOT fulfil the order.
# Only good qty at the DISPATCH step (or the last step if no DISPATCH) counts.
# --------------------------------------------------------------------------- #
# A routing whose last step is a DISPATCH pass-through (5 of the 7 focus items).
_DISPATCH_ROUTING = _routing(
    "A", [("CNC FIRST SIDE", "CNC4"), ("WASHING", None), ("DISPATCH", None)]
)
# A routing whose last real step is PACKING and has NO DISPATCH (the other 2 items).
_PACKING_ROUTING = _routing(
    "B", [("CNC FIRST SIDE", "CNC4"), ("PACKING", "MPK1")]
)


def test_finished_gate_is_dispatch_when_present():
    assert orderbook.finished_gate(_DISPATCH_ROUTING) == "DISPATCH"


def test_finished_gate_falls_back_to_last_step_without_dispatch():
    assert orderbook.finished_gate(_PACKING_ROUTING) == "PACKING"


def test_finished_good_ignores_intermediate_process():
    masters = _masters(_DISPATCH_ROUTING)
    # 5 good produced at the FIRST process -> WIP, fulfils nothing.
    wip = _actual("A", "CNC FIRST SIDE", qty_produced=5)
    assert orderbook.finished_good_by_order([wip], masters) == {}


def test_finished_good_counts_at_dispatch():
    masters = _masters(_DISPATCH_ROUTING)
    done = _actual("A", "DISPATCH", qty_produced=5)
    assert orderbook.finished_good_by_order([done], masters) == {("SO1", "A"): 5.0}


def test_finished_good_counts_at_packing_when_no_dispatch():
    masters = _masters(_PACKING_ROUTING)
    done = Actual(so_no="SO2", item_code="B", entry_date=D, process="PACKING",
                  qty_produced=8, qty_rejected=1)        # good = 7
    assert orderbook.finished_good_by_order([done], masters) == {("SO2", "B"): 7.0}


def test_finished_gate_match_is_normalized():
    masters = _masters(_DISPATCH_ROUTING)
    # operator typed it lower-case with stray spaces — must still match.
    done = _actual("A", "  dispatch ", qty_produced=3)
    assert orderbook.finished_good_by_order([done], masters) == {("SO1", "A"): 3.0}


def test_active_so_lines_ignore_wip_keep_full_qty():
    """The user's bug: 5 good at the first process must NOT shrink the order."""
    active = {("SO1", "A"): _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    wip = _actual("A", "CNC FIRST SIDE", qty_produced=5)
    lines = orderbook.active_so_lines(active, [wip], masters)
    assert {l.so_no: l.qty for l in lines} == {"SO1": 500.0}     # full 500 still planned


def test_active_so_lines_reduce_only_on_dispatch():
    active = {("SO1", "A"): _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    done = _actual("A", "DISPATCH", qty_produced=5)
    lines = orderbook.active_so_lines(active, [done], masters)
    assert {l.so_no: l.qty for l in lines} == {"SO1": 495.0}


def test_status_running_on_any_actual_even_if_only_wip():
    """Work has started (first process) -> Running, even though nothing is finished."""
    o = _order("SO1", "A", 500, D)
    with_actuals = orderbook.orders_with_actuals(
        [_actual("A", "CNC FIRST SIDE", qty_produced=5)]
    )
    assert orderbook.derive_status(o, with_actuals) == orderbook.RUNNING
    o.completed = True
    assert orderbook.derive_status(o, with_actuals) == orderbook.COMPLETE


def test_status_pending_with_no_actuals():
    o = _order("SO1", "A", 500, D)
    assert orderbook.derive_status(o, orderbook.orders_with_actuals([])) == orderbook.PENDING


# --------------------------------------------------------------------------- #
# Per-process progress: each process must be re-planned at ordered − done-at-that
# -process, so finished steps aren't redone ("continue from reality").
# --------------------------------------------------------------------------- #
def test_completed_by_process_sums_good_per_step():
    acts = [_actual("A", "CNC FIRST SIDE", qty_produced=50),
            _actual("A", "cnc first side ", qty_produced=10),   # normalized -> same step
            _actual("A", "WASHING", qty_produced=20, qty_rejected=2)]
    cbp = orderbook.completed_by_process(acts)
    assert cbp[("SO1", "A", "CNC FIRST SIDE")] == 60.0
    assert cbp[("SO1", "A", "WASHING")] == 18.0                 # good = 20 − 2


def test_completed_by_process_separates_same_process_across_items():
    """Same SO#, same process name, two different items — kept apart by item code."""
    acts = [Actual("SO1", "A", D, process="CNC", qty_produced=30),
            Actual("SO1", "B", D, process="CNC", qty_produced=40)]
    cbp = orderbook.completed_by_process(acts)
    assert cbp[("SO1", "A", "CNC")] == 30.0
    assert cbp[("SO1", "B", "CNC")] == 40.0


def test_active_so_lines_carry_per_process_remaining():
    active = {("SO1", "A"): _order("SO1", "A", 500, D)}   # routing: CNC FIRST SIDE, WASHING, DISPATCH
    masters = _masters(_DISPATCH_ROUTING)
    acts = [_actual("A", "CNC FIRST SIDE", qty_produced=50),
            _actual("A", "WASHING", qty_produced=20)]
    line = orderbook.active_so_lines(active, acts, masters)[0]
    assert line.qty == 500                            # order remaining (nothing at DISPATCH gate yet)
    assert line.process_qty["CNC FIRST SIDE"] == 450  # 500 − 50 already cut
    assert line.process_qty["WASHING"] == 480         # 500 − 20
    assert line.process_qty["DISPATCH"] == 500        # gate: 0 done


def test_active_so_lines_no_actuals_leave_process_qty_none():
    # No actuals -> no per-process override -> Rule 6 keeps today's behaviour (golden stable).
    active = {("SO1", "A"): _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    line = orderbook.active_so_lines(active, [], masters)[0]
    assert line.process_qty is None


def test_process_progress_rows_show_done_and_remaining_per_step():
    active = {("SO1", "A"): _order("SO1", "A", 500, D)}   # routing: CNC FIRST SIDE, WASHING, DISPATCH
    masters = _masters(_DISPATCH_ROUTING)
    acts = [_actual("A", "CNC FIRST SIDE", qty_produced=50),
            _actual("A", "WASHING", qty_produced=20)]
    rows = orderbook.process_progress_rows(active, acts, masters)
    # one row per process of each order that has started; ordered by seq.
    first = next(r for r in rows if r["Process"] == "CNC FIRST SIDE")
    assert first["SO No"] == "SO1" and first["Completed"] == 50 and first["Remaining"] == 450
    wash = next(r for r in rows if r["Process"] == "WASHING")
    assert wash["Completed"] == 20 and wash["Remaining"] == 480


def test_process_progress_rows_empty_without_actuals():
    active = {("SO1", "A"): _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    assert orderbook.process_progress_rows(active, [], masters) == []


# Recorded downtime / setup time is captured for the record only and never affects
# the schedule (the feedback loop is quantity-only), so there is no downtime→plan
# attribution to test here.


# --------------------------------------------------------------------------- #
# effective_plan_start_date: the plan clock advances past days already worked, so a
# re-plan continues from the next working day's first shift (not the original date).
# --------------------------------------------------------------------------- #
_CAL = WorkCalendar()   # Thursday (weekday 3) off, no holidays


def _act(entry):
    return Actual(so_no="SO1", item_code="A", entry_date=entry, qty_produced=1)


def test_plan_start_no_actuals_is_the_config_date():
    assert orderbook.effective_plan_start_date([], date(2025, 3, 1), _CAL) == date(2025, 3, 1)


def test_plan_start_advances_to_day_after_latest_actual():
    # Punch dated 1 Mar (Sat) → plan continues 2 Mar (Sun is a working day; only Thu off).
    assert orderbook.effective_plan_start_date([_act(date(2025, 3, 1))],
                                               date(2025, 3, 1), _CAL) == date(2025, 3, 2)


def test_plan_start_skips_the_weekly_off_day():
    # Punch dated 5 Mar (Wed) → next is 6 Mar (Thursday, off) → lands on 7 Mar (Fri).
    assert orderbook.effective_plan_start_date([_act(date(2025, 3, 5))],
                                               date(2025, 3, 1), _CAL) == date(2025, 3, 7)


def test_plan_start_uses_the_latest_of_many_actuals():
    acts = [_act(date(2025, 3, 1)), _act(date(2025, 3, 3)), _act(date(2025, 3, 2))]
    assert orderbook.effective_plan_start_date(acts, date(2025, 3, 1), _CAL) == date(2025, 3, 4)


def test_plan_start_never_goes_before_the_config_date():
    # An old actual must not drag the plan earlier than its configured start.
    assert orderbook.effective_plan_start_date([_act(date(2025, 3, 1))],
                                               date(2025, 3, 10), _CAL) == date(2025, 3, 10)


# --------------------------------------------------------------------------- #
# Capture-actuals list stays small: only the latest punched date's entries are
# shown / rollback-able; earlier days are locked (kept in the record).
# --------------------------------------------------------------------------- #
def test_latest_actual_date_and_filter_to_latest():
    a1 = Actual(so_no="SO1", item_code="A", entry_date=date(2025, 3, 1), qty_produced=1)
    a2 = Actual(so_no="SO2", item_code="B", entry_date=date(2025, 3, 2), qty_produced=1)
    a3 = Actual(so_no="SO3", item_code="C", entry_date=date(2025, 3, 2), qty_produced=1)
    assert orderbook.latest_actual_date([a1, a2, a3]) == date(2025, 3, 2)
    latest = orderbook.actuals_on_latest_date([a1, a2, a3])
    assert {a.so_no for a in latest} == {"SO2", "SO3"}     # only 2 March, not SO1


def test_latest_actual_helpers_empty():
    assert orderbook.latest_actual_date([]) is None
    assert orderbook.actuals_on_latest_date([]) == []


# --------------------------------------------------------------------------- #
# Rejection accounting: rejects must reduce the cumulative good, so rejected
# pieces stay to be REDONE (director's spec). Net is summed then clamped >= 0.
# --------------------------------------------------------------------------- #
def test_completed_by_process_nets_rejections_across_entries():
    a1 = Actual(so_no="SO1", item_code="A", entry_date=date(2025, 3, 1),
                process="CNC", qty_produced=100, qty_rejected=0)
    a2 = Actual(so_no="SO1", item_code="A", entry_date=date(2025, 3, 2),
                process="CNC", qty_produced=0, qty_rejected=20)   # rework scrap next day
    assert orderbook.completed_by_process([a1, a2]) == {("SO1", "A", "CNC"): 80.0}


def test_completed_by_process_clamps_net_negative_to_zero():
    a = Actual(so_no="SO1", item_code="A", entry_date=date(2025, 3, 1),
               process="CNC", qty_produced=5, qty_rejected=8)
    assert orderbook.completed_by_process([a]).get(("SO1", "A", "CNC"), 0) == 0.0


def test_finished_good_nets_rejections_across_entries():
    masters = _masters(_routing("A", [("CNC", "M1"), ("PACKING", "MPK1")]))   # gate = PACKING
    acts = [Actual(so_no="SO1", item_code="A", entry_date=date(2025, 3, 1),
                   process="PACKING", qty_produced=50),
            Actual(so_no="SO1", item_code="A", entry_date=date(2025, 3, 2),
                   process="PACKING", qty_produced=0, qty_rejected=10)]
    assert orderbook.finished_good_by_order(acts, masters) == {("SO1", "A"): 40.0}


def _seq_routing(*names):
    procs = [Process(seq=i + 1, name=n, cycle_time=1, total_time=1,
                     suggested_machine="M", allotted_machine=None)
             for i, n in enumerate(names)]
    return Routing(item_code="X", description="", customer="", rm_type="", moq=None,
                   processes=procs)


def test_is_dispatch_matches_misspelling():
    assert orderbook.is_dispatch("DISPATCH")
    assert orderbook.is_dispatch("Dispatch")
    assert orderbook.is_dispatch("DISAPTCH")      # transposed misspelling in the real data
    assert not orderbook.is_dispatch("BANDSAW OS")
    assert not orderbook.is_dispatch("PACKING")


def test_finished_gate_uses_misspelled_dispatch():
    # DISAPTCH is the gate even when it is NOT the last step.
    r = _seq_routing("OP", "DISAPTCH", "STRAGGLER")
    assert orderbook.finished_gate(r) == "DISAPTCH"


def test_finished_gate_falls_back_to_last_step_seq_helper():
    r = _seq_routing("OP", "PACKING")
    assert orderbook.finished_gate(r) == "PACKING"


# --- Feedback precedence guardrail (2026-07-25 spec) ----------------------- #
def _pg_routing():
    return Routing(item_code="X", description="", customer="", rm_type="", moq=None,
                   processes=[Process(1, "CNC FIRST SIDE", 5, 5, "CNC1", "CNC1"),
                              Process(2, "VMC FIRST SIDE", 6, 6, "VMC1", "VMC1")])


def _pg_act(proc, prod, rej=0):
    return Actual(so_no="S1", item_code="X", entry_date=date(2025, 3, 1),
                  qty_produced=prod, qty_rejected=rej, process=proc, item_name="X", operator="o")


def test_pg_cannot_punch_downstream_before_upstream():
    err = orderbook.precedence_cap_error([_pg_act("VMC FIRST SIDE", 10)],
                                         "S1", "X", "VMC FIRST SIDE", _pg_routing(), 40)
    assert err and "CNC FIRST SIDE" in err


def test_pg_downstream_capped_at_upstream_good():
    ok = [_pg_act("CNC FIRST SIDE", 20), _pg_act("VMC FIRST SIDE", 20)]
    assert orderbook.precedence_cap_error(ok, "S1", "X", "VMC FIRST SIDE", _pg_routing(), 40) is None
    bad = [_pg_act("CNC FIRST SIDE", 20), _pg_act("VMC FIRST SIDE", 21)]
    assert orderbook.precedence_cap_error(bad, "S1", "X", "VMC FIRST SIDE", _pg_routing(), 40) is not None


def test_pg_first_process_capped_at_ordered_qty():
    assert orderbook.precedence_cap_error([_pg_act("CNC FIRST SIDE", 41)],
                                          "S1", "X", "CNC FIRST SIDE", _pg_routing(), 40) is not None
    assert orderbook.precedence_cap_error([_pg_act("CNC FIRST SIDE", 40)],
                                          "S1", "X", "CNC FIRST SIDE", _pg_routing(), 40) is None


def test_pg_rejects_reduce_good_passed_downstream():
    base = [_pg_act("CNC FIRST SIDE", 25, 5)]   # good 20
    assert orderbook.precedence_cap_error(base + [_pg_act("VMC FIRST SIDE", 20)],
                                          "S1", "X", "VMC FIRST SIDE", _pg_routing(), 40) is None
    assert orderbook.precedence_cap_error(base + [_pg_act("VMC FIRST SIDE", 21)],
                                          "S1", "X", "VMC FIRST SIDE", _pg_routing(), 40) is not None


def test_pg_no_routing_or_unknown_process_allows():
    assert orderbook.precedence_cap_error([_pg_act("CNC FIRST SIDE", 99)], "S1", "X",
                                          "CNC FIRST SIDE", None, 40) is None
    assert orderbook.precedence_cap_error([_pg_act("MYSTERY", 99)], "S1", "X",
                                          "MYSTERY", _pg_routing(), 40) is None


def test_pg_other_orders_do_not_interfere():
    other = Actual(so_no="S2", item_code="X", entry_date=date(2025, 3, 1),
                   qty_produced=100, qty_rejected=0, process="CNC FIRST SIDE",
                   item_name="X", operator="o")
    assert orderbook.precedence_cap_error([other, _pg_act("CNC FIRST SIDE", 40)],
                                          "S1", "X", "CNC FIRST SIDE", _pg_routing(), 40) is None


def test_pg_rollback_blocked_when_downstream_depends():
    after = [_pg_act("CNC FIRST SIDE", 10), _pg_act("VMC FIRST SIDE", 20)]
    err = orderbook.rollback_cap_error(after, _pg_act("CNC FIRST SIDE", 10), _pg_routing())
    assert err and "VMC FIRST SIDE" in err


def test_pg_rollback_allowed_when_no_dependency():
    after = [_pg_act("CNC FIRST SIDE", 10)]
    assert orderbook.rollback_cap_error(after, _pg_act("CNC FIRST SIDE", 10), _pg_routing()) is None


def test_pg_rollback_of_last_process_allowed():
    after = [_pg_act("CNC FIRST SIDE", 20), _pg_act("VMC FIRST SIDE", 10)]
    assert orderbook.rollback_cap_error(after, _pg_act("VMC FIRST SIDE", 10), _pg_routing()) is None
