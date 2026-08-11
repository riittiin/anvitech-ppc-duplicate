"""Rule 1 — Consolidate sales orders.

Group SO lines of the SAME item code whose delivery dates fall within a
configurable window (default 10 days) into one production batch.

Pure function: ``run(so_lines, config, notes, masters) -> list[Batch]``.

Algorithm (per item code): sort that item's lines by delivery date, then walk
them greedily — a line joins the current batch if its delivery date is within
``window`` days of the batch's GOVERNING (earliest) date; otherwise it opens a
new batch. The governing date is the earliest delivery in the batch (it drives
priority in Rule 2).
"""
from __future__ import annotations

from collections import defaultdict

from ..models import Batch, fmt_date


def run(so_lines, config=None, notes=None, masters=None, **kw):
    notes = notes if notes is not None else []
    window = config.consolidation_window_days if config else 10

    by_item = defaultdict(list)
    for so in so_lines:
        by_item[so.item_code].append(so)

    batches = []
    batch_counter = 0
    for item_code in sorted(by_item.keys()):
        # Lines sorted by SO delivery date; the batch's SO delivery date is the
        # earliest one in the group (the binding customer commitment).
        lines = sorted(by_item[item_code], key=lambda s: s.delivery_date)
        current = None
        for line in lines:
            if current is None or (line.delivery_date - current["so_date"]).days > window:
                if current is not None:
                    batches.append(_finalize(current, batch_counter))
                    batch_counter += 1
                current = {
                    "item_code": item_code,
                    "item_name": line.item_name,
                    "so_date": line.delivery_date,
                    "qty": line.qty,
                    "so_refs": [line.so_no],
                    "lines": [line],
                }
            else:
                current["qty"] += line.qty
                current["so_refs"].append(line.so_no)
                current["lines"].append(line)
                gap = (line.delivery_date - current["so_date"]).days
                notes.append(
                    f"{item_code}: SO {line.so_no} (SO delivery date "
                    f"{fmt_date(line.delivery_date)}) clubbed into batch with SO "
                    f"delivery date {fmt_date(current['so_date'])} "
                    f"({gap} day(s) apart, within {window}-day window)"
                )
        if current is not None:
            batches.append(_finalize(current, batch_counter))
            batch_counter += 1

    # Note the singletons / splits for visibility.
    for b in batches:
        if len(b.source_so_refs) == 1:
            notes.append(
                f"{b.item_code}: SO {b.source_so_refs[0]} stands alone "
                f"(no other line within {window} days)"
            )

    return batches


def _merge_process_qty(lines):
    """Sum the per-process remaining vectors of the clubbed lines. A line with no
    progress (process_qty None) contributes its full qty to every process. If no
    line carries progress, return None (run the full batch qty everywhere)."""
    if all(getattr(l, "process_qty", None) is None for l in lines):
        return None
    keys = set()
    for l in lines:
        if l.process_qty:
            keys |= set(l.process_qty)
    return {k: sum((l.process_qty.get(k, l.qty) if l.process_qty else l.qty) for l in lines)
            for k in keys}


def _batch_commitment(lines):
    """The tightest committed promise among the batch's member SO-lines: the
    EARLIEST ``promised_date`` of any line with ``commitment == "committed"``.
    Returns ``("committed", date)`` if any such line exists, else ``("open",
    None)`` — the Batch defaults, unchanged for an all-open group."""
    dates = [
        l.promised_date
        for l in lines
        if getattr(l, "commitment", "open") == "committed" and l.promised_date is not None
    ]
    if dates:
        return "committed", min(dates)
    return "open", None


def _finalize(cur, idx) -> Batch:
    commitment, promised_date = _batch_commitment(cur["lines"])
    return Batch(
        batch_id=f"B{idx + 1:03d}",
        item_code=cur["item_code"],
        item_name=cur["item_name"],
        qty=cur["qty"],
        so_delivery_date=cur["so_date"],
        source_so_refs=list(cur["so_refs"]),
        process_qty=_merge_process_qty(cur["lines"]),
        commitment=commitment,
        promised_date=promised_date,
    )
