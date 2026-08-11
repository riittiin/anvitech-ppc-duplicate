"""Rule 2 — Sort batches by SO delivery date (primary priority).

Order all batches by their SO delivery date, soonest first. Stable sort, so
same-date batches keep Rule 1's order until Rule 3 breaks ties.

Pure function: ``run(batches, config, notes, masters) -> list[Batch]``.
"""
from __future__ import annotations

from ..models import fmt_date


def run(batches, config=None, notes=None, masters=None, **kw):
    notes = notes if notes is not None else []
    ordered = sorted(batches, key=lambda b: b.so_delivery_date)
    notes.append(
        "Sorted by SO delivery date (earliest first): "
        + " < ".join(f"{b.item_code}@{fmt_date(b.so_delivery_date)}" for b in ordered)
    )
    return ordered
