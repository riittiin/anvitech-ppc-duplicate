# Change an SO delivery date by re-importing the Excel (2026-08-04)

## Problem

Anvitech's directors revise customer delivery dates. Today the only way that
reaches the app is a fresh order, because a re-imported `(SO#, item code)` is
refused: `engine/orderbook.py:254-261` flags it
`"changed: original kept (revisions deferred)"` and keeps the original. The
refusal is deliberate and correct as a default — it protects live production
data from a stale spreadsheet — but it leaves no way at all to move a date.

Directors want to edit **SO Delivery Date** in the SO list they already upload,
re-import, and have that one field change.

## Decisions taken (owner, 2026-08-04)

1. **Delivery date only.** A changed quantity or item name is still detected and
   reported, never applied. Quantity is entangled with recorded production
   (remaining = ordered − good produced), so silently changing it could make an
   order look over-produced and would shift the arithmetic under every plan.
2. **The applied optimization is KEPT**, not discarded. A date edit must not
   throw away a 15-30 minute search — the unoptimized ordering is measurably
   worse (hundreds of late-days on Test8). The plan keeps running the searched
   sequence, and a banner says it is out of date.
3. **Re-sequencing is manual.** The banner tells the admin the applied
   optimization no longer matches current delivery dates; they press **Start
   deep search** when they choose to.

## Design

### 1. `merge_upload` learns to update a date

`engine/orderbook.merge_upload` returns **`(new_orders, updated_orders, flags)`**
— a third element, because the API counts `len(new_orders)` as "added" and an
update is not an addition. It stays pure: it returns the updated `Order` objects
(built with `dataclasses.replace(existing, delivery_date=new)`) and mutates
nothing.

Per re-imported active `(SO#, item)`:

| Uploaded row | Result |
|---|---|
| delivery date differs, and is a real date | `Order` copy with the new date in `updated_orders`; flag `delivery date updated: 30-08-2026 → 15-09-2026` |
| delivery date same, qty/name differ | no update; flag `changed: only the delivery date can be updated by re-import` |
| nothing differs | no update; flag `duplicate: already in the book` (unchanged) |
| delivery date blank or unreadable | no update; flag `delivery date missing or unreadable — kept the existing date` |

`replace()` preserves every other field by construction: `ordered_qty`,
`item_name`, `commitment`, `promised_date`, `committed_at`, `completed`,
`first_seen`. Recorded production is untouched because actuals are stored
separately and keyed by the same `(SO#, item)` — the order keeps its identity, so
punches, per-process progress and any frozen in-progress operation all still
point at it.

**Completed orders are still skipped** (`already completed: not re-added`). They
are archived and excluded from planning; moving their date changes nothing.

**A committed order's `promised_date` does not move.** The customer's delivery
date and what Anvitech promised are different facts, and the +3-day committed
promise ceiling stays anchored to the promise. Only `delivery_date` changes.

### 2. `api/main.py` `/upload` persists the updates

`book_store.add_orders(new_orders + updated_orders)` — `add_orders` already
writes by key with `hset`, so an updated order overwrites in place. No new
storage function. The response gains `"updated": len(updated_orders)` beside
`"added"`, and the existing flags list carries the human-readable old → new line,
which `web/app.js:313-316` already renders after an upload.

### 3. Delivery dates enter the book fingerprint

`optimize_service.book_signature` (line 136-141) currently hashes so_no,
item_code, qty, process_qty, commitment and promised_date — **not**
`delivery_date`. Left alone, a date-only edit would leave the signature
identical, so `_try_start_auto()` would conclude "nothing material changed" and
skip: the daily "Done entering — update plan" would refuse to re-sequence around
a date the directors just changed.

Adding `str(l.delivery_date)` to each row fixes that. Consequence to expect once:
every existing book's signature changes, so the first "Done" after deploy runs
one contest it would otherwise have skipped. Harmless.

### 4. The staleness banner

`_optimize_apply` already stores `book_sig` and `inputs_sig` in the applied
plan's meta. It gains **`dates`** — a plain `{"<so>\x1f<item>": "YYYY-MM-DD"}`
map of the delivery dates the optimization was computed against.

`_plan` compares that map against the current SO-lines over the **intersection of
keys** and reports `dates_changed` (bool) plus `dates_changed_count` in
`optimize_meta`. Intersection matters: comparing whole maps would fire the banner
every time an order completes or a new order arrives, which is normal traffic and
not a reason to re-optimize.

A stored map rather than a hash, because it costs about 3 KB for a 70-order book
and lets the banner say *how many* orders moved instead of just "something
changed".

This is **self-correcting**: it compares live data, so reverting the date in
Excel clears the banner by itself. There is no flag anyone must remember to
reset.

`web/app.js` adds one warning beside the existing `inputs_changed` one:

> N order(s) have a delivery date that changed since the applied optimization —
> the job order no longer reflects them. Run **Start deep search**.

Applying any optimization rewrites `dates` and clears the banner.

### 5. What deliberately does not change

- No new trigger. Upload still never starts a contest.
- The pure engine, scheduler and optimizer are untouched; a plan computed from
  the same book is byte-identical.
- No UI for editing dates in the app. Excel stays the single source, as asked.

## Testing

**`tests/test_orderbook.py`** (pure, the core of the feature):
date differs → updated order returned with only `delivery_date` changed and every
other field identical; flag names old and new date; identical row → no update;
qty-only change → flagged, NOT updated; blank/`None` uploaded date → no update
and the existing date survives; completed order → still skipped; intra-upload
duplicate behaviour unchanged.

**`tests/test_api.py`** (round trip): upload, punch some production, upload again
with a changed date → `GET /orders` shows the new date, the punches and the
derived Running status survive, and a committed order keeps its `promised_date`
and `commitment`.

**`tests/test_optimize_service.py`**: two books differing only by a delivery date
produce different `book_signature`s.

**Staleness**: apply an optimization, change one delivery date, plan →
`optimize_meta.dates_changed` is True with the right count; revert the date →
False; apply again → False.

**Regression**: the full suite (741 currently) stays green, including the golden
trace — nothing in the scheduling path moves.
