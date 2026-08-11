# SO Delivery Date Re-import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a director change an SO Delivery Date by editing the Excel SO list and re-uploading, instead of the re-import being refused outright.

**Architecture:** `engine/orderbook.merge_upload` (pure) gains a third return value, `updated_orders` — copies of existing orders with only `delivery_date` changed. `api/main.py` persists them through the existing `book_store.add_orders` (which overwrites by key). `delivery_date` joins `optimize_service.book_signature` so the daily auto-optimize stops wrongly skipping a date-only edit. The applied optimization records the delivery dates it was computed against, and the plan compares them to raise a staleness banner.

**Tech Stack:** Python 3, FastAPI, plain HTML/JS frontend, pytest.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-04-so-delivery-date-reimport-design.md`. Read it before Task 1.
- **Delivery date is the ONLY field a re-import may change.** Quantity, item name, `commitment`, `promised_date`, `committed_at`, `completed` and `first_seen` are never touched by an upload.
- **Completed orders stay skipped.** A `(SO#, item)` in the completed archive is still flagged `already completed: not re-added` and never updated.
- **The pure engine, scheduler and optimizer are not modified.** A plan computed from the same book must stay byte-identical. The golden trace test must not move.
- **Dates display as DD-MM-YYYY** in every user-facing string, via `engine.models.fmt_date`. ISO (`YYYY-MM-DD`) is for storage only.
- **Order identity is the `(so_no, item_code)` pair**, never the SO number alone.
- **Run the full suite** (`python3 -m pytest -q`, currently 741 passing, 1 skipped) before the final commit of each task. Use `python3`, not `python` — `python` is not on PATH on this machine.

---

### Task 1: `merge_upload` updates the delivery date

**Files:**
- Modify: `engine/orderbook.py:232-271` (the `merge_upload` function)
- Test: `tests/test_orderbook.py` (existing merge tests at lines 27-65 need their unpacking updated)

**Interfaces:**
- Consumes: `engine.models.Order`, `engine.models.fmt_date` (already imported at `engine/orderbook.py:15`)
- Produces: `merge_upload(so_lines, active_orders, completed_orders, first_seen="") -> (new_orders: list[Order], updated_orders: list[Order], flags: list[dict])`. Task 2 depends on this exact 3-tuple order.

- [ ] **Step 1: Update the existing tests to unpack three values**

Five call sites in `tests/test_orderbook.py` currently unpack two values. Change each to unpack three. The middle value is `updated` and is `[]` in all five existing cases:

```python
# line ~31
new, updated, flags = orderbook.merge_upload(lines, active, completed)
assert updated == []
# line ~43
new, updated, flags = orderbook.merge_upload([_so("SO1", "B", 20, D)], active, {})
# line ~47
new2, updated2, flags2 = orderbook.merge_upload([_so("SO1", "A", 10, D)], active, {})
# line ~53  (this test's assertions change — see Step 2)
# line ~62
new, updated, flags = orderbook.merge_upload(lines, {}, {})
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_orderbook.py`. Also REPLACE the existing `test_merge_flags_changed_order_without_modifying` (line 51-57) with the qty-only version below, because its old reason text no longer applies:

```python
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


def test_merge_does_not_update_quantity_only_the_date():
    active = {("SO1", "A"): _order("SO1", "A", 10, D)}
    new, updated, flags = orderbook.merge_upload([_so("SO1", "A", 99, D)], active, {})
    assert new == [] and updated == []
    assert flags[0]["reason"] == "changed: only the delivery date can be updated by re-import"
    assert active[("SO1", "A")].ordered_qty == 10


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
    assert flags[0]["reason"] == ("delivery date missing or unreadable — "
                                 "kept the existing date")
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_orderbook.py -q`
Expected: FAIL — `ValueError: not enough values to unpack (expected 3, got 2)`.

- [ ] **Step 4: Implement**

In `engine/orderbook.py`, add to the imports near line 9:

```python
from dataclasses import replace
```

Replace the body of `merge_upload` (lines 232-271) with:

```python
def merge_upload(so_lines, active_orders: dict, completed_orders: dict, first_seen: str = ""):
    """Merge uploaded SO lines into the book. Pure — returns
    ``(new_orders, updated_orders, flags)`` and does not mutate the inputs.

    Identity is the **(SO number, item code)** pair, never the SO number alone: one
    SO number can carry several item lines and each is its own order. So SO1/A and
    SO1/B are two distinct orders, and only an exact (SO#, item) repeat is a repeat.

    * unseen (SO#, item) -> a new Pending order
    * active (SO#, item) with a DIFFERENT delivery date -> an updated copy in
      ``updated_orders`` (2026-08-04: directors revise delivery dates in the Excel
      and re-import). ``delivery_date`` is the ONLY field an upload may change —
      quantity is entangled with recorded production (remaining = ordered − good),
      so silently changing it could make an order look over-produced.
    * active (SO#, item) with the same date -> flagged only, original untouched
    * (SO#, item) in the completed archive -> flagged, never updated: it is
      archived and out of planning, so moving its date achieves nothing

    Each flag carries both ``so_no`` and ``item_code`` so the report is unambiguous.
    """
    new_orders, updated_orders, flags = [], [], []
    seen_in_upload = set()

    def _d(value):
        return fmt_date(value) if value else "none"

    for so in so_lines:
        key = so.key                       # (so_no, item_code)
        base = {"so_no": so.so_no, "item_code": so.item_code}
        if key in seen_in_upload:
            flags.append({**base, "reason": "duplicate (SO#, item) within this upload"})
            continue
        # Claim the key for EVERY branch, not just new orders: a key repeated in one
        # file must never be updated twice.
        seen_in_upload.add(key)
        if key in active_orders:
            ex = active_orders[key]
            if so.delivery_date is None:
                # A blank/unparseable date cell must never wipe a real date.
                reason = "delivery date missing or unreadable — kept the existing date"
            elif ex.delivery_date != so.delivery_date:
                updated_orders.append(replace(ex, delivery_date=so.delivery_date))
                reason = (f"delivery date updated: {_d(ex.delivery_date)} → "
                          f"{_d(so.delivery_date)}")
            elif ex.ordered_qty != so.qty or ex.item_name != so.item_name:
                reason = "changed: only the delivery date can be updated by re-import"
            else:
                reason = "duplicate: already in the book"
            flags.append({**base, "reason": reason})
        elif key in completed_orders:
            flags.append({**base, "reason": "already completed: not re-added"})
        else:
            new_orders.append(Order(
                so_no=so.so_no, item_code=so.item_code, item_name=so.item_name,
                ordered_qty=so.qty, delivery_date=so.delivery_date,
                completed=False, first_seen=first_seen,
            ))
    return new_orders, updated_orders, flags
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_orderbook.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q`
Expected: one failure only, in `tests/test_api.py` or wherever `/upload` is exercised, because `api/main.py:1847` still unpacks two values. Task 2 fixes it. If anything ELSE fails, stop and investigate before continuing.

- [ ] **Step 7: Commit**

```bash
git add engine/orderbook.py tests/test_orderbook.py
git commit -m "feat(orderbook): re-importing an SO line updates its delivery date"
```

---

### Task 2: `/upload` persists the updated orders

**Files:**
- Modify: `api/main.py:1846-1859` (the `/upload` handler tail)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `merge_upload(...) -> (new_orders, updated_orders, flags)` from Task 1
- Produces: the `/upload` JSON response gains `"updated": <int>`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api.py`. Match the file's existing client/upload helpers — read the top of the file first and reuse whatever it already uses to log in and post a workbook, rather than inventing new helpers:

```python
def test_reupload_with_a_changed_delivery_date_updates_the_order(monkeypatch):
    """A director edits SO Delivery Date in Excel and re-imports: the date moves,
    the recorded production and the order's identity do not."""
    import datetime
    import io
    import openpyxl
    from tests.sample_workbook import build_workbook

    m = _api()                      # use this module's existing helper
    admin = _admin_client(m)        # use this module's existing helper

    admin.post("/upload", files={"file": ("t.xlsx", build_sample_bytes(),
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    before = admin.get("/orders").json()["orders"]
    assert before, "upload should have seeded the book"

    # Rebuild the same workbook with SO1's delivery date pushed out by 30 days.
    wb = build_workbook()
    ws = wb["Sales Order (SO) list"]
    old = ws.cell(row=2, column=24).value          # 'SO Delivery Date' column
    ws.cell(row=2, column=24).value = old + datetime.timedelta(days=30)
    buf = io.BytesIO(); wb.save(buf)

    r = admin.post("/upload", files={"file": ("t2.xlsx", buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    body = r.json()
    assert body["added"] == 0          # no new orders
    assert body["updated"] == 1        # exactly the one changed row
    assert any("delivery date updated" in f["reason"] for f in body["flagged"])

    after = {(o["SO No"], o["Item Code"]): o for o in admin.get("/orders").json()["orders"]}
    assert len(after) == len(before)   # no duplicate order was created
```

Before writing this test, run `python3 -m pytest tests/test_api.py -q --collect-only | head` and read the top ~60 lines of `tests/test_api.py` to get the real helper names and the exact `/orders` row key spelling. Adjust the row-key lookup to whatever `order_rows` actually emits — do not guess.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_api.py -q -k reupload`
Expected: FAIL — `ValueError: not enough values to unpack` (the handler still unpacks two).

- [ ] **Step 3: Implement**

In `api/main.py`, replace lines 1847-1854:

```python
    new_orders, updated_orders, flags = orderbook.merge_upload(
        so_lines, active, completed, first_seen=_ist_today().isoformat())
    # `add_orders` writes by (SO#, item) with hset, so an updated order overwrites
    # in place — an update needs no separate storage path.
    book_store.add_orders(new_orders + updated_orders)

    result = {
        "name": file.filename,
        "added": len(new_orders),
        "updated": len(updated_orders),
        "flagged": flags,
```

Leave the rest of the dict as it is.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_api.py -q -k reupload`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass (741 + the new tests).

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_api.py
git commit -m "feat(api): persist delivery-date updates on re-upload"
```

---

### Task 3: `delivery_date` joins the book fingerprint

**Files:**
- Modify: `engine/optimize_service.py:136-141` (`book_signature`)
- Test: `tests/test_optimize_service.py`

**Interfaces:**
- Consumes: nothing new
- Produces: no signature change — `book_signature(so_lines, absences=None, frozen=None)` keeps its shape, only its hash input widens

**Why this task exists:** `book_signature` hashes so_no, item_code, qty, process_qty, commitment and promised_date — not `delivery_date`. Without this, a date-only edit leaves the signature identical, so `_try_start_auto()` concludes "nothing material changed" and the daily "Done entering — update plan" refuses to re-sequence around the new date. It also feeds `_plan_fingerprint` (`api/main.py:1076`), so without it the plan cache would serve a stale plan computed on the old date.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_optimize_service.py`, reusing whatever SOLine helper that file already defines:

```python
def test_book_signature_changes_when_a_delivery_date_changes():
    """A director's date edit must count as a material book change, or the daily
    auto-optimize skips it and the job order never reflects the new date."""
    import datetime
    from engine.models import SOLine
    from engine import optimize_service

    a = SOLine(so_no="SO1", item_code="A", item_name="A", qty=10,
               delivery_date=datetime.date(2025, 8, 1))
    b = SOLine(so_no="SO1", item_code="A", item_name="A", qty=10,
               delivery_date=datetime.date(2025, 9, 15))

    assert optimize_service.book_signature([a]) != optimize_service.book_signature([b])
    # Same book, same signature — still deterministic.
    assert optimize_service.book_signature([a]) == optimize_service.book_signature([a])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_optimize_service.py -q -k delivery_date`
Expected: FAIL — the two signatures are equal.

- [ ] **Step 3: Implement**

In `engine/optimize_service.py`, add `delivery_date` to the row tuple at lines 137-141:

```python
    rows = sorted(
        (l.so_no, l.item_code, round(float(l.qty), 3),
         json.dumps(l.process_qty or {}, sort_keys=True, default=str),
         getattr(l, "commitment", "open") or "open",
         str(getattr(l, "promised_date", None)),
         # 2026-08-04: a re-import can change the SO delivery date. Without it here
         # the auto trigger would call a date-only edit "nothing changed" and never
         # re-sequence around the new date.
         str(getattr(l, "delivery_date", None)))
        for l in so_lines)
```

Then update the docstring's back-compat sentence (lines 132-135), which currently promises byte-identical signatures to the pre-`frozen` era. Add:

```
    Note: adding ``delivery_date`` (2026-08-04) changed the hash for every book,
    so the first auto-optimize after that deploy runs one contest it would
    otherwise have skipped. Harmless, and one-time.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_optimize_service.py -q -k delivery_date`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass. Some tests may assert stored signature strings — if any fail, they are asserting a hash literal and must be updated to compare signatures to each other rather than to a baked constant. Do NOT weaken a test that is checking real behaviour.

- [ ] **Step 6: Commit**

```bash
git add engine/optimize_service.py tests/test_optimize_service.py
git commit -m "fix(optimize): count a changed SO delivery date as a material book change"
```

---

### Task 4: The applied optimization records the dates it was computed against

**Files:**
- Modify: `api/main.py` — add a `_delivery_dates()` helper next to `_current_book_sig` (line 1057), extend the apply meta (line 1773-1779), extend `optimize_meta` (line 822-832)
- Test: `tests/test_report_and_staleness.py`

**Interfaces:**
- Consumes: `KEY_SEP` (already imported at `api/main.py:44`)
- Produces: `_delivery_dates() -> dict[str, str]` mapping `"<so_no>\x1f<item_code>"` to an ISO date string; `optimize_meta` gains `"dates_changed": bool` and `"dates_changed_count": int`, which Task 5 renders

- [ ] **Step 1: Write the failing test**

Add to `tests/test_report_and_staleness.py`, reusing that file's existing client and seeding helpers:

```python
def test_optimize_meta_flags_a_changed_delivery_date(monkeypatch):
    """After an optimization is applied, changing an order's delivery date must
    mark the applied plan stale so the admin knows to run Start deep search."""
    m = _api()                          # use this file's existing helper
    admin = _admin_client(m)            # use this file's existing helper
    _seed_book()                        # use this file's existing helper

    # Save an applied optimization whose recorded dates match the book.
    dates = m._delivery_dates()
    assert dates, "the seeded book should have at least one order"
    key = sorted(dates)[0]
    m.book_store.save_plan_priority({key: 0}, {"saved_at": "2026-08-04T10:00:00",
                                               "dates": dict(dates)})

    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["dates_changed"] is False
    assert meta["dates_changed_count"] == 0

    # Move that order's delivery date.
    so_no, item_code = key.split(m.KEY_SEP)
    active = m.book_store.load_active_orders()
    order = active[(so_no, item_code)]
    import dataclasses
    import datetime
    m.book_store.add_orders([dataclasses.replace(
        order, delivery_date=order.delivery_date + datetime.timedelta(days=30))])

    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["dates_changed"] is True
    assert meta["dates_changed_count"] == 1


def test_optimize_meta_ignores_orders_the_optimization_never_saw(monkeypatch):
    """Only orders present in BOTH the applied snapshot and the current book are
    compared. A newly uploaded or newly completed order is normal traffic, not a
    reason to tell the admin their optimization is stale."""
    m = _api()
    admin = _admin_client(m)
    _seed_book()

    dates = m._delivery_dates()
    key = sorted(dates)[0]
    stale = dict(dates)
    stale["SO_GONE" + m.KEY_SEP + "ITEM_GONE"] = "2020-01-01"   # not in the book
    m.book_store.save_plan_priority({key: 0}, {"saved_at": "2026-08-04T10:00:00",
                                               "dates": stale})

    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["dates_changed"] is False
```

Read the top of `tests/test_report_and_staleness.py` first and reuse its real helper names; the `_api` / `_admin_client` / `_seed_book` names above follow the convention in `tests/test_operators_api.py` but must be verified, not assumed.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_report_and_staleness.py -q -k delivery_date`
Expected: FAIL — `AttributeError: module 'api.main' has no attribute '_delivery_dates'`.

- [ ] **Step 3: Implement the helper**

In `api/main.py`, immediately after `_current_book_sig` (ends line 1063), add:

```python
def _delivery_dates() -> dict:
    """Every active order's delivery date, keyed like the optimizer's ranks.

    Stored alongside an applied optimization so a later plan can tell whether the
    delivery dates have moved since it was computed (a director re-importing the
    Excel with a revised date). A plain map rather than a hash: it costs ~3 KB for
    a 70-order book and lets the banner say HOW MANY orders moved."""
    return {f"{o.so_no}{KEY_SEP}{o.item_code}": o.delivery_date.isoformat()
            for o in book_store.load_active_orders().values()
            if not o.completed and o.delivery_date}
```

- [ ] **Step 4: Record the dates when an optimization is applied**

In `api/main.py`, in the `meta` dict at lines 1773-1779, add one entry after `"book_sig"`:

```python
                "book_sig": _current_book_sig(),
                # The delivery dates this optimization was computed against, so a
                # later plan can flag "the dates moved, re-run the deep search".
                "dates": _delivery_dates()}
```

- [ ] **Step 5: Compare them on every plan**

In `api/main.py`, inside the `if prio:` block at lines 823-832, add before the `optimize_meta = {...}` assignment:

```python
        # Delivery-date staleness. Compare only keys present in BOTH the applied
        # snapshot and the current book: an order that has since completed or been
        # newly uploaded is normal traffic, not a reason to cry stale.
        saved_dates = (prio.get("meta") or {}).get("dates") or {}
        current_dates = {f"{l.so_no}{KEY_SEP}{l.item_code}":
                         l.delivery_date.isoformat() if l.delivery_date else None
                         for l in so_lines}
        dates_moved = sum(1 for k, v in saved_dates.items()
                          if k in current_dates and current_dates[k] != v)
```

and add two entries to the `optimize_meta` dict:

```python
                         "inputs_changed": bool(saved_sig and saved_sig != current_inputs_sig),
                         "dates_changed": dates_moved > 0,
                         "dates_changed_count": dates_moved}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_report_and_staleness.py -q -k delivery_date`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add api/main.py tests/test_report_and_staleness.py
git commit -m "feat(api): flag an applied optimization stale when delivery dates move"
```

---

### Task 5: The banner

**Files:**
- Modify: `web/app.js:444-447` (the `warns` block)

**Interfaces:**
- Consumes: `optimizeMeta.dates_changed` and `optimizeMeta.dates_changed_count` from Task 4

There is no JS test harness in this repo, so this task is verified in a real browser.

- [ ] **Step 1: Implement**

In `web/app.js`, directly after the existing `inputs_changed` push (line 445-447), add:

```js
  if (optimizeMeta && optimizeMeta.dates_changed) {
    const n = optimizeMeta.dates_changed_count || 0;
    warns.push(`${n} order${n === 1 ? "" : "s"} ${n === 1 ? "has" : "have"} a delivery `
      + "date that changed since the applied optimization — the job order no longer "
      + "reflects them. Run Start deep search.");
  }
```

- [ ] **Step 2: Check the syntax**

Run: `node --check web/app.js`
Expected: no output (valid).

- [ ] **Step 3: Verify in a browser**

Start an isolated instance (never point at the real store):

```bash
SCRATCH=/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/a4f4ef11-847b-4ead-8ae3-319343a45bdf/scratchpad
mkdir -p $SCRATCH/store2
STORE_DIR=$SCRATCH/store2 AUTO_OPTIMIZE=0 DEFAULT_SCHEDULER=new \
  ADMIN_USERNAME=t_admin ADMIN_PASSWORD=t_pass_12345 \
  USER_USERNAME=t_user USER_PASSWORD=t_user_12345 \
  python3 -m uvicorn api.main:app --port 8112
```

Then, with the gstack browse binary at `~/.claude/skills/gstack/browse/dist/browse`:
1. Log in as `t_admin` / `t_pass_12345`, upload `Test8.xlsx`.
2. Edit one SO Delivery Date in a copy of the workbook, re-upload, and confirm the upload status line shows `delivery date updated: DD-MM-YYYY → DD-MM-YYYY`.
3. Confirm the Orders tab shows the new date and the order count did not grow.
4. Save a fake applied optimization whose `dates` map holds the OLD date, re-plan, and confirm the warning chip shows the new sentence.
5. Kill the server and delete `$SCRATCH/store2`.

- [ ] **Step 4: Commit**

```bash
git add web/app.js
git commit -m "feat(web): warn when delivery dates moved since the applied optimization"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` (the `engine/orderbook.py` bullet and the `api/main.py` `/upload` mention)

- [ ] **Step 1: Update `CLAUDE.md`**

In the `engine/orderbook.py` bullet, change the `merge_upload` description from "add new / flag repeat / flag completed / intra-upload dedup" to note the new behaviour and the 3-tuple return:

```
`merge_upload` (add new / **update an active line's delivery date** / flag
repeat / flag completed / intra-upload dedup, all by the **(SO#, item code)**
pair) returns `(new_orders, updated_orders, flags)`. **Delivery date is the ONLY
field a re-import may change** (2026-08-04,
`docs/superpowers/specs/2026-08-04-so-delivery-date-reimport-design.md`) —
directors revise SO Delivery Date in the Excel and re-import; quantity is
entangled with recorded production so it stays report-only. A blank/unreadable
uploaded date never wipes an existing one, and a completed order is never
updated. `optimize_service.book_signature` now includes `delivery_date` (so the
daily auto-optimize stops calling a date-only edit "nothing changed"), and an
applied optimization stores a `dates` map in its meta which `_plan` compares —
over the INTERSECTION of keys, so a completed or newly added order never
false-alarms — to raise `optimize_meta.dates_changed`/`dates_changed_count` and
the "run Start deep search" banner. The applied plan is deliberately KEPT, not
cleared, so one date edit can never discard a searched plan.
```

- [ ] **Step 2: Run the full suite one final time**

Run: `python3 -m pytest -q`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the delivery-date re-import behaviour"
```

---

## Self-Review

**Spec coverage:**
- Update delivery date only → Task 1
- Four report outcomes with old → new → Task 1
- Blank date never wipes → Task 1
- Completed skipped → Task 1
- Committed `promised_date` unmoved → Task 1 (asserted directly)
- `/upload` persists, response gains `updated` → Task 2
- `delivery_date` in `book_signature` → Task 3
- `dates` map on apply, intersection comparison, `dates_changed` → Task 4
- Banner text → Task 5
- Applied optimization kept, not cleared → no task needed; nothing clears it, and Task 4 only reports
- No new trigger on upload → no task needed; `/upload` still never calls `_try_start_auto`

**Type consistency:** `merge_upload` returns the 3-tuple in Task 1 and is unpacked in that order in Task 2. `_delivery_dates()` returns `{str: str}` in Task 4 and is compared against ISO strings built the same way in the same task. `dates_changed` / `dates_changed_count` are named identically in Tasks 4 and 5.

**Known follow-through for the implementer:** the test helper names quoted for `tests/test_api.py` and `tests/test_report_and_staleness.py` follow this repo's convention but MUST be read from those files, not assumed. Tasks 2 and 4 both say so explicitly.
