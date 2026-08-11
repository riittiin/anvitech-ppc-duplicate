"""In-app Operator & Shift master + Friday rotation (pure).

The Excel "Operator & shift Master" sheet is only ever used to **seed** this
table once (see the spec, ``docs/superpowers/specs/2026-07-18-operator-master-
rotation-design.md``). After that, operators live in the durable store
(``engine/book_store.py``) and a re-upload never touches them.

Purity: every function here takes ``today: date`` as an explicit parameter.
No ``datetime.now()`` / ``date.today()`` calls inside this module — callers
(the API layer) supply "now" so the logic stays deterministic and testable.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from . import loaders

FRIDAY = 4  # date.weekday(): Monday=0 ... Sunday=6


def seed_rows_from_masters(masters) -> list:
    """One-time seed: copy the workbook's operators into the app-owned table
    shape. ``pinned`` starts False for everyone; each row gets a fresh id."""
    return [
        {
            "id": uuid.uuid4().hex,
            "name": op.name,
            "machines_raw": op.preferred_machines_raw,
            "shift": op.shift,
            "pinned": False,
        }
        for op in masters.operators
    ]


def last_friday(today: date) -> date:
    """The most recent Friday on or before ``today`` (Friday itself if today
    is a Friday)."""
    days_since = (today.weekday() - FRIDAY) % 7
    return today - timedelta(days=days_since)


def next_rotation(today: date) -> date:
    """The first Friday strictly after ``today``."""
    days_until = (FRIDAY - today.weekday()) % 7
    if days_until == 0:
        days_until = 7
    return today + timedelta(days=days_until)


def _parse_iso_date(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def _fridays_after(anchor: date, today: date) -> list:
    """Every Friday strictly after ``anchor`` up to and including ``today``,
    in order. Empty if none fall in that window."""
    out = []
    candidate = anchor + timedelta(days=(FRIDAY - anchor.weekday()) % 7)
    if candidate <= anchor:
        candidate += timedelta(days=7)
    while candidate <= today:
        out.append(candidate)
        candidate += timedelta(days=7)
    return out


def _is_two_shift(shift: str) -> bool:
    """A row is two-shift if its shift text contains "first"/"second",
    case-insensitively — matches the case-insensitive normalization
    engine.efficiency already applies, so e.g. "first shift" (lowercase)
    rotates the same as "First shift"."""
    low = (shift or "").lower()
    return "first" in low or "second" in low


def _flip_shift(shift: str) -> str:
    low = (shift or "").lower()
    if "first" in low:
        return "Second shift"
    if "second" in low:
        return "First shift"
    return shift


def rotate_table(table: dict, today: date):
    """No-op since 2026-08-05: operator shifts no longer rotate.

    The shift an admin sets in Settings is the shift the planner uses, every week,
    until an admin changes it. Kept (rather than deleted) because it is the shared
    expression every wiring site calls through -- `operators_as_of`, the display
    overlay, the contest setup -- and because its `(new_table, flips_applied)`
    contract is unpacked by those callers. Always returns the table untouched and
    zero flips, so the stored `week_anchor` and per-row `pinned` fields are inert.
    """
    return table, 0


def operators_as_of(table: dict, as_of: date) -> list:
    """The effective ``Operator`` objects for a plan/display computed **as of**
    ``as_of`` — a PURE view (rotation applied, nothing persisted). Combines
    ``rotate_table`` + ``to_operators`` so every wiring site (display overlay,
    contest setup, ``_plan``) shares one expression instead of duplicating it."""
    return to_operators(rotate_table(table, as_of)[0].get("operators", []))


def to_operators(rows: list) -> list:
    """Convert stored operator rows to ``Operator`` objects, parsing
    ``machines_raw`` exactly the way the Excel loader does (same helper:
    ``loaders.parse_resource_candidates``) so a seeded table is
    indistinguishable from one loaded straight from the workbook."""
    from .models import Operator  # local import: avoid a cycle with models

    out = []
    for row in rows:
        raw = row.get("machines_raw", "") or ""
        out.append(
            Operator(
                name=row.get("name", ""),
                preferred_machines_raw=raw,
                machines=loaders.parse_resource_candidates(raw),
                shift=row.get("shift", "") or "",
            )
        )
    return out
