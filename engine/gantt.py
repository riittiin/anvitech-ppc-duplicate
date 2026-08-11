"""Gantt view-model — turns the Rule 6 schedule into the shop's
'Sample entry window format' layout: one row per order, a day-column timeline,
process bars positioned by real time and coloured by machine.

Bars are positioned with fractional-day offsets (from the actual start/end
datetimes), so a process that runs 08:00-14:00 sits in the first part of its
day and the nights/off-days show as gaps — a true Gantt, not just day buckets.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# Light fills; first three echo the sample sheet (peach / green / yellow).
PALETTE = [
    "#F4CCAC", "#A9D08E", "#FFE699", "#9DC3E6", "#C9A0DC", "#F8A978",
    "#B5E6B5", "#FFD966", "#8FBCE0", "#D5A6BD", "#C6E0B4", "#FCE4A6",
]


def _empty():
    return {"axis_start": None, "num_days": 0, "days": [], "months": [],
            "rows": [], "machine_colors": {}}


def _row_status(source_so_refs, item_code, status_by_order):
    """Status for one Gantt row. A batch consolidates several SO lines that all
    share this ``item_code`` (Rule 1 groups by item), so each order is looked up by
    the ``(SO number, item code)`` pair — not the SO number alone, which isn't unique."""
    if not status_by_order:
        return ""
    statuses = {status_by_order.get((sn, item_code))
                for sn in source_so_refs if status_by_order.get((sn, item_code))}
    if len(statuses) == 1:
        return next(iter(statuses))
    return "Mixed" if statuses else ""


def _bar_operator(e):
    """Operator label for one bar — the shared ``ScheduleEntry.operator_label`` so the
    Gantt, the Schedule table and the machine-wise view can never disagree on who runs
    a job (a shift handoff shows "Alpha → Bravo"; a single-shift op shows one name)."""
    return e.operator_label()


def build_gantt(schedule, batches, masters, status_by_order=None):
    if not schedule:
        return _empty()

    axis_date = min(e.start.date() for e in schedule)
    last_date = max(e.end.date() for e in schedule)
    axis_mid = datetime(axis_date.year, axis_date.month, axis_date.day)
    num_days = (last_date - axis_date).days + 1

    # Day + month header model.
    days, months = [], []
    for i in range(num_days):
        d = axis_date + timedelta(days=i)
        label = d.strftime("%b-%y")
        days.append({
            "day": d.day,
            "date": d.isoformat(),
            "month": label,
            "working": masters.calendar.is_working_day(d),
        })
        if months and months[-1]["label"] == label:
            months[-1]["days"] += 1
        else:
            months.append({"label": label, "days": 1})

    # Colour per machine (stable, first-appearance order).
    def disp(mid):
        m = masters.machines.get(mid)
        return m.display_name if m else mid

    machine_order = []
    for e in schedule:
        if e.machine not in machine_order:
            machine_order.append(e.machine)
    color_by_id = {mid: PALETTE[i % len(PALETTE)] for i, mid in enumerate(machine_order)}

    # Group the schedule into one row per batch (order).
    bmap = {b.batch_id: b for b in batches}
    by_batch, order = {}, []
    for e in schedule:
        if e.batch_id not in by_batch:
            by_batch[e.batch_id] = []
            order.append(e.batch_id)
        by_batch[e.batch_id].append(e)

    rows = []
    for bid in order:
        entries = sorted(by_batch[bid], key=lambda x: x.start)
        b = bmap.get(bid)
        bars = []
        for e in entries:
            offset = (e.start - axis_mid).total_seconds() / 86400.0
            duration = max((e.end - e.start).total_seconds() / 86400.0, 0.0)
            bars.append({
                "process": e.process_name,
                "machine": disp(e.machine),
                "operator": _bar_operator(e),
                "color": color_by_id[e.machine],
                "offset_days": round(offset, 4),
                "duration_days": round(duration, 4),
                "start": e.start.strftime("%d-%m-%Y %H:%M"),
                "end": e.end.strftime("%d-%m-%Y %H:%M"),
                "qty": e.qty,
            })
        # Expected completion = when the LAST process of this order finishes (the
        # latest end across all its bars — not entries[-1], which is sorted by start).
        completion = max(e.end for e in entries)
        rows.append({
            "batch_id": bid,
            "item_name": (b.item_name if b else ""),
            "item_code": (b.item_code if b else entries[0].item_code),
            "so_no": (", ".join(b.source_so_refs) if b else ""),
            "so_qty": (b.qty if b else ""),
            "so_delivery_date": (b.so_delivery_date.strftime("%d-%m-%Y") if b else ""),
            "completion": completion.strftime("%d-%m-%Y"),
            "status": _row_status(b.source_so_refs if b else [],
                                  (b.item_code if b else entries[0].item_code),
                                  status_by_order),
            "bars": bars,
        })

    return {
        "axis_start": axis_date.isoformat(),
        "num_days": num_days,
        "days": days,
        "months": months,
        "rows": rows,
        "machine_colors": {disp(mid): c for mid, c in color_by_id.items()},
    }
