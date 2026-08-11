# Hide the commit/uncommit feature behind one switch (2026-08-04)

## Why

Anvitech's directors decided they don't want the Committed/Open lane feature for
now. They explicitly want it **hidden, not deleted** — they may ask for it back,
and when they do it should return immediately.

At the time of writing **no order in the book is committed**, which is what makes
this safe to do without touching the engine.

## The switch

`api/main.py`:

```python
COMMITMENT_FEATURE_ENABLED = False   # flip to True to bring the lanes back
```

Served to the browser on `GET /me` (already called at boot) as
`commitment_enabled`, so the UI and the server can never disagree about whether
the feature is on. There is exactly one source of truth.

## What is hidden when it is off

**Frontend** (`web/app.js`):
- the **Commit selected** / **Uncommit selected** buttons and their wiring
- the **Lane** and **Promised** columns in the Orders table, and the red
  "slipped" flag that compares Promised against the current expected date
- the sentence explaining lanes in the Orders footnote

**Frontend** (`web/index.html`): the Optimize panel's line "Open orders are
sequenced freely; committed orders are held within +3 days of their promised
date."

**Server** (`api/main.py`): `POST /orders/commit` and `POST /orders/uncommit`
return **404** while the feature is off. This is the part that matters — with the
buttons gone but the endpoints live, an order could still be committed through
the API and would then steer the optimizer (weight 5000 in
`engine/optimizer.py:94`) with nothing on screen to reveal or undo it. Closing the
endpoints makes that impossible rather than merely unlikely.

## What is deliberately NOT touched

The engine, `engine/optimizer.py`'s promise penalty, the ppc objective mirror,
`Order.commitment` / `promised_date` / `committed_at`, `book_store`'s commitment
writers, `orderbook.split_committed_open`, and every existing promise test. All of
it stays live and green.

Because no order is committed, that machinery is dormant: **the schedule does not
change by a single minute.** This is the whole reason the feature can be hidden
without an engine change — verified by the fact that the promise penalty is keyed
on `l.commitment == "committed"` (`engine/optimizer.py:153`), which no order
satisfies.

Stored commitment data on any order is left untouched, so nothing is lost.

## Bringing it back

Set `COMMITMENT_FEATURE_ENABLED = True`. Buttons, columns, footnote, Optimize
text and both endpoints all return together, and any order committed previously
still carries its lane and promise exactly as it was. No migration, no rebuild.

## Testing

- `GET /me` reports `commitment_enabled: False` while off, `True` while on.
- `POST /orders/commit` and `POST /orders/uncommit` return 404 while off.
- Both endpoints still work when the flag is monkeypatched on — proving the
  feature is hidden, not broken.
- The existing promise/commitment tests (`test_committed_promise_metric.py`,
  `test_promise_backstop.py`, `test_commit_endpoints.py`,
  `test_manual_apply_backstop.py`, `test_worst_ceiling.py`) stay green, because
  the engine is untouched. Any test that drives the endpoints enables the flag.
- Browser check: no Commit/Uncommit buttons, no Lane or Promised column, and the
  Orders table still renders correctly for both roles.
- Full suite green; golden trace unmoved.
