"""Rule 7 — Capture daily actuals.

After each shift, the previous period's production (qty produced/rejected, actual
setup, the six downtime categories, remarks, and an optional "mark order complete"
flag) is entered and persisted via the durable store (append-only — see
``book_store``). This is the only data the app writes besides the order book.

``run`` appends one or more entries and returns the full, current list. The
order-completion side effect (when ``mark_complete`` is set) is applied by the API
layer, not here, so this stays focused on actuals.
"""
from __future__ import annotations

from ..models import Actual, fmt_date
from .. import book_store


def load_actuals() -> list:
    return book_store.load_actuals()


def aggregate_by_item(actuals):
    """Roll up output + downtime per item code — where each entry's minutes 'get
    added for that item code' (e.g. all No-operator minutes summed)."""
    agg = {}
    order = []
    for a in actuals:
        if a.item_code not in agg:
            order.append(a.item_code)
            agg[a.item_code] = {
                "Item Code": a.item_code, "Item Name": a.item_name, "Entries": 0,
                "Qty Produced": 0.0, "Qty Rejected": 0.0, "Good Qty": 0.0,
                "Setup (min)": 0.0, "No Power": 0.0, "No Operator": 0.0,
                "Tool Problem": 0.0, "M/c Breakdown": 0.0, "No Load": 0.0,
                "Other Work": 0.0, "Total Downtime (min)": 0.0,
            }
        d = agg[a.item_code]
        if not d["Item Name"] and a.item_name:
            d["Item Name"] = a.item_name
        d["Entries"] += 1
        d["Qty Produced"] += a.qty_produced
        d["Qty Rejected"] += a.qty_rejected
        d["Good Qty"] += a.good_qty()
        d["Setup (min)"] += a.actual_setup_min
        d["No Power"] += a.no_power_min
        d["No Operator"] += a.no_operator_min
        d["Tool Problem"] += a.tool_problem_min
        d["M/c Breakdown"] += a.machine_breakdown_min
        d["No Load"] += a.no_load_min
        d["Other Work"] += a.other_work_min
        d["Total Downtime (min)"] += a.total_downtime_min()
    return [agg[c] for c in order]


def run(new_entries, config=None, notes=None, masters=None, **kw):
    """Append ``new_entries`` (Actual or list[Actual]) to the store; return all."""
    notes = notes if notes is not None else []
    if isinstance(new_entries, Actual):
        new_entries = [new_entries]
    for a in (new_entries or []):
        book_store.append_actual(a)
        notes.append(
            f"Recorded {a.qty_produced:g} produced / {a.qty_rejected:g} rejected "
            f"(good {a.good_qty():g}) for {a.item_code} (SO {a.so_no}) on "
            f"{fmt_date(a.entry_date)}; setup {a.actual_setup_min:g} min, "
            f"downtime {a.total_downtime_min():g} min"
            + (": MARKED COMPLETE" if a.mark_complete else "")
        )
    return book_store.load_actuals()
