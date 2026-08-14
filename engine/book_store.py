"""Persistence for the order book, actuals, and uploaded masters.

Thin layer over ``storage.get_store()``. Orders are a hash keyed by the
**(SO number, item code)** pair — encoded as a single composite string, since a
hash field must be a string (see ``_skey``) — because an SO number is NOT unique;
only the pair is. Per-field writes never clash; actuals are an append-only list
(no overwrite); the latest uploaded workbook is a single value. All durable in
production (Upstash) and on local disk in dev.
"""
from __future__ import annotations

import base64
import json
import uuid

from .models import Order, Actual
from .storage import get_store

ORDERS_KEY = "anvitech:orders"             # hash: "so_no\x1fitem_code" -> Order json
COMPLETED_KEY = "anvitech:orders:completed"  # hash: same composite field (archive)
ACTUALS_KEY = "anvitech:actuals"           # list of Actual json
MASTERS_KEY = "anvitech:masters"           # kv: base64 of the latest workbook
PLAN_CONFIG_KEY = "anvitech:plan_config"   # kv: json of the admin's saved Config
PLAN_PRIORITY_KEY = "anvitech:plan_priority"  # kv: json {ranks, meta} of the applied Optimize run
AUTO_NOTE_KEY = "anvitech:auto_note"        # kv: json note from the self-tuning trigger (informational)
LAST_SEARCHED_KEY = "anvitech:last_searched"  # kv: json {book_sig, inputs_sig} of the last completed contest
ABSENCES_KEY = "anvitech:absences"          # kv: json list of operator absences
OPERATORS_KEY = "anvitech:operators"        # kv: json {week_anchor, operators:[...]}
LAST_APPLIED_SCHEDULE_KEY = "anvitech:last_applied_schedule"  # kv: json list of applied-schedule op rows
FROZEN_OPS_KEY = "anvitech:frozen_ops"       # kv: json list of frozen (in-progress) op rows for today
CP_GENOME_KEY = "anvitech:cp_genome"         # kv: json of the CP engine's applied decision genome
PLAN_START_FLOOR_KEY = "anvitech:plan_start_floor"  # kv: json {date, floor} — today's pinned auto start

_SEP = "\x1f"   # ASCII unit separator — never appears in an SO# or item code


def _skey(so_no: str, item_code: str) -> str:
    """The hash-field string that stores one order = its (SO#, item) pair. Using a
    control char keeps it collision-free vs any real SO#/item text."""
    return f"{so_no}{_SEP}{item_code}"


# --- orders --- #
def load_active_orders() -> dict:
    """Active orders keyed by the ``(so_no, item_code)`` tuple (the order's identity)."""
    h = get_store().hgetall(ORDERS_KEY)
    orders = [Order.from_json(json.loads(v)) for v in h.values()]
    return {o.key: o for o in orders}


def load_completed_orders() -> dict:
    h = get_store().hgetall(COMPLETED_KEY)
    orders = [Order.from_json(json.loads(v)) for v in h.values()]
    return {o.key: o for o in orders}


def add_orders(orders) -> None:
    s = get_store()
    for o in orders:
        s.hset(ORDERS_KEY, _skey(o.so_no, o.item_code), json.dumps(o.to_json()))


def delete_orders(order_keys) -> int:
    """Permanently delete orders (active or archived) by their ``(so_no, item_code)``
    pair, and purge those orders' production actuals. Returns how many order keys
    were targeted. Only the exact item line is removed — a sibling line sharing the
    SO number is left intact."""
    s = get_store()
    targets = {tuple(k) for k in order_keys}
    for so_no, item_code in targets:
        s.hdel(ORDERS_KEY, _skey(so_no, item_code))
        s.hdel(COMPLETED_KEY, _skey(so_no, item_code))
    remaining = [a for a in load_actuals() if a.key not in targets]
    s.list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in remaining])
    return len(targets)


def delete_all() -> None:
    """Wipe all orders + actuals (keeps the uploaded masters)."""
    s = get_store()
    s.delete_key(ORDERS_KEY)
    s.delete_key(COMPLETED_KEY)
    s.delete_key(ACTUALS_KEY)


def complete_order(so_no: str, item_code: str) -> bool:
    """Move one active order — the ``(so_no, item_code)`` line — into the completed
    archive. Returns False if unknown. A sibling item line on the same SO is
    unaffected."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.completed = True
    field = _skey(so_no, item_code)
    s.hset(COMPLETED_KEY, field, json.dumps(o.to_json()))
    s.hdel(ORDERS_KEY, field)
    return True


def uncomplete_order(so_no: str, item_code: str) -> bool:
    """Move a completed order — the ``(so_no, item_code)`` line — BACK to active
    (un-archive). Returns False if it isn't in the completed archive. Used when a
    'mark complete' entry is rolled back."""
    s = get_store()
    o = load_completed_orders().get((so_no, item_code))
    if o is None:
        return False
    o.completed = False
    field = _skey(so_no, item_code)
    s.hset(ORDERS_KEY, field, json.dumps(o.to_json()))
    s.hdel(COMPLETED_KEY, field)
    return True


def set_commitment(so_no: str, item_code: str, commitment: str,
                   promised_date, committed_at: str) -> bool:
    """Set an ACTIVE order's commitment lane + promised date. `commitment` is
    'committed'; `promised_date` is a date or None; `committed_at` is an
    ISO datetime string (passed in so this stays deterministic). False if unknown."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.commitment = commitment
    o.promised_date = promised_date
    o.committed_at = committed_at
    s.hset(ORDERS_KEY, _skey(so_no, item_code), json.dumps(o.to_json()))
    return True


def clear_commitment(so_no: str, item_code: str) -> bool:
    """Reset an active order back to the Open lane (clears promise). False if unknown."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.commitment = "open"
    o.promised_date = None
    o.committed_at = None
    s.hset(ORDERS_KEY, _skey(so_no, item_code), json.dumps(o.to_json()))
    return True


# --- actuals (append-safe) --- #
def load_actuals() -> list:
    """Load all actuals. Backfills a stable id on any legacy entry that lacks one
    (one-time persist) so rollback can target an exact entry."""
    raw = get_store().list_all(ACTUALS_KEY)
    actuals = [Actual.from_json(json.loads(v)) for v in raw]
    if any(not a.id for a in actuals):
        for a in actuals:
            if not a.id:
                a.id = uuid.uuid4().hex
        get_store().list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in actuals])
    return actuals


def append_actual(actual: Actual) -> None:
    if not actual.id:
        actual.id = uuid.uuid4().hex
    get_store().list_append(ACTUALS_KEY, json.dumps(actual.to_json()))


def delete_actual(actual_id: str):
    """Remove the actual with ``actual_id``. Returns the removed Actual, or None
    if no entry matched."""
    actuals = load_actuals()
    target = next((a for a in actuals if a.id == actual_id), None)
    if target is None:
        return None
    remaining = [a for a in actuals if a.id != actual_id]
    get_store().list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in remaining])
    return target


# --- masters workbook --- #
def save_masters_bytes(raw: bytes) -> None:
    get_store().kv_set(MASTERS_KEY, base64.b64encode(raw).decode("ascii"))


def load_masters_bytes():
    raw = get_store().kv_get(MASTERS_KEY)
    return base64.b64decode(raw) if raw else None


# --- plan config (admin's saved scheduling settings) --- #
def save_plan_config(raw_json: str) -> None:
    get_store().kv_set(PLAN_CONFIG_KEY, raw_json)


def load_plan_config():
    return get_store().kv_get(PLAN_CONFIG_KEY)


# --- plan priority (the applied Optimize run) --- #
def save_plan_priority(ranks: dict, meta: dict) -> None:
    """Persist an applied optimization: ``ranks`` maps the composite order key
    "<so>\x1f<item>" -> 1-based rank; ``meta`` records saved_at, scores, budget,
    seed and the covered keys, for the staleness banner."""
    get_store().kv_set(PLAN_PRIORITY_KEY, json.dumps({"ranks": ranks, "meta": meta}))


def load_plan_priority():
    """The applied optimization as {"ranks": {...}, "meta": {...}}, or None.
    A corrupt/legacy value is treated as absent (plans fall back to pure Rule 3)."""
    raw = get_store().kv_get(PLAN_PRIORITY_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ranks"), dict) or not data["ranks"]:
        return None
    return data


def clear_plan_priority() -> None:
    get_store().delete_key(PLAN_PRIORITY_KEY)


# --- self-tuning trigger note (informational, non-blocking) --- #
def save_auto_note(note: dict) -> None:
    get_store().kv_set(AUTO_NOTE_KEY, json.dumps(note))


def load_auto_note():
    raw = get_store().kv_get(AUTO_NOTE_KEY)
    return json.loads(raw) if raw else None


# --- pinned auto plan-start floor (one per day) --- #
def save_plan_start_floor(day_iso: str, floor_iso: str) -> None:
    """Pin the day's auto plan start. Written once per day by ``api._resolve_config``
    so every run that day — every tab, every download, every optimize candidate —
    plans from the SAME clock. See ``load_plan_start_floor``."""
    get_store().kv_set(PLAN_START_FLOOR_KEY,
                       json.dumps({"date": day_iso, "floor": floor_iso}))


def load_plan_start_floor():
    """``{"date": ISO, "floor": ISO}`` or None. The caller uses it only when its
    ``date`` is still today; a stale day is ignored, not cleaned up."""
    raw = get_store().kv_get(PLAN_START_FLOOR_KEY)
    return json.loads(raw) if raw else None


# --- last-searched marker (informational, non-blocking) --- #
def save_last_searched(sig: dict) -> None:
    get_store().kv_set(LAST_SEARCHED_KEY, json.dumps(sig))


def load_last_searched():
    raw = get_store().kv_get(LAST_SEARCHED_KEY)
    return json.loads(raw) if raw else None


# --- last-applied schedule (the plan the floor is following) --- #
def save_last_applied_schedule(rows: list) -> None:
    """Persist the applied plan's per-op assignment (machine/operator/time). Written
    only when an optimize result is APPLIED — never on a display re-plan, so it stays
    'the plan the floor is following' and doesn't drift with new actuals."""
    get_store().kv_set(LAST_APPLIED_SCHEDULE_KEY, json.dumps(rows))


def load_last_applied_schedule() -> list:
    raw = get_store().kv_get(LAST_APPLIED_SCHEDULE_KEY)
    return json.loads(raw) if raw else []


# --- frozen (in-progress) ops for the current day --- #
def save_frozen_ops(rows: list) -> None:
    get_store().kv_set(FROZEN_OPS_KEY, json.dumps(rows))


def load_frozen_ops() -> list:
    raw = get_store().kv_get(FROZEN_OPS_KEY)
    return json.loads(raw) if raw else []


def clear_frozen_ops() -> None:
    get_store().delete_key(FROZEN_OPS_KEY)


# --- the CP engine's applied decision genome --- #
# Its OWN key, deliberately: no other engine reads it, so rolling the deploy back
# to DEFAULT_SCHEDULER=roster leaves nothing to migrate (plan §W.5). It is written
# only when an optimize result is APPLIED, and read on every plan by
# api._resolve_config, which attaches it to the config the seam receives.
def save_cp_genome(g: dict | None) -> None:
    """Persist the solved genome, flattened for JSON exactly once.

    ``cp_engine.genome.to_json`` is **NOT idempotent**: handed an already-flat
    genome it rebuilds each key from the FIRST TWO CHARACTERS of the string
    (``"CNC1\\x1f1"`` -> ``("C", "N")``), because it assumes every tuple-keyed
    value is still tuple-keyed. That is not an exception and not a malformed
    genome — every later lookup simply MISSES, the decoder falls back for every
    single operation, and the plan looks perfectly well-formed. A genome arrives
    already flat whenever it came home over the wire (a cloud worker's row), so
    the shape is checked here rather than assumed.
    """
    from cp_engine.genome import _TUPLE_KEYED, to_json
    if not g:
        get_store().kv_set(CP_GENOME_KEY, json.dumps({}))
        return
    flat = any(isinstance(key, str)
               for name in _TUPLE_KEYED
               for key in (g.get(name) or {}))
    get_store().kv_set(CP_GENOME_KEY, json.dumps(g if flat else to_json(g)))


def load_cp_genome() -> dict:
    """The stored genome AS WRITTEN — flat, JSON-shaped. ``engine.cp_adapter._genome``
    is the one door that converts it back to tuple keys, and it detects the shape
    itself, so this deliberately does not decode: two decoders would be two
    definitions."""
    raw = get_store().kv_get(CP_GENOME_KEY)
    return json.loads(raw) if raw else {}


def clear_cp_genome() -> None:
    get_store().delete_key(CP_GENOME_KEY)


# --- operator absences --- #
def load_absences() -> list:
    raw = get_store().kv_get(ABSENCES_KEY)
    return json.loads(raw) if raw else []


def save_absence(a: dict) -> dict:
    a = {"id": uuid.uuid4().hex, "operator": a["operator"],
         "from_date": a["from_date"], "to_date": a["to_date"]}
    rows = load_absences() + [a]
    get_store().kv_set(ABSENCES_KEY, json.dumps(rows))
    return a


def delete_absence(absence_id: str) -> bool:
    rows = load_absences()
    keep = [r for r in rows if r.get("id") != absence_id]
    if len(keep) == len(rows):
        return False
    get_store().kv_set(ABSENCES_KEY, json.dumps(keep))
    return True


# --- operator master (app-owned, seeded once from the workbook) --- #
def load_operator_table():
    """The app-owned operator/shift table, or ``None`` if never seeded."""
    raw = get_store().kv_get(OPERATORS_KEY)
    return json.loads(raw) if raw else None


def save_operator_table(table: dict) -> None:
    get_store().kv_set(OPERATORS_KEY, json.dumps(table))
