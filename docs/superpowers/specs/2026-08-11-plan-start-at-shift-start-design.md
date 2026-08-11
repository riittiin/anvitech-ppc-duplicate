# Plan start returns to the shift start (08:00), not the next hour

**Date:** 2026-08-11
**Owner decision.** Reverts the 2026-08-03 "next full hour" plan clock
(`e57aef7` + `6eda289`) and the stored-clock refinement it grew on 2026-08-07
(`c145d80`), behind a single off-by-default switch.

## The ask

> "Whenever we click Optimize, the schedule plans and the entire schedule starts
> from the next hour — click at 10:30 Tuesday, the schedule starts 11:00 Tuesday.
> Since yesterday's experiment I observed that if the jobs need the full timeline,
> I would prefer it to start at the normal shift. Press Optimize at 09:30 and the
> schedule starts at 08:00. Return to the previous behaviour."

Confirmed with the owner: **always 08:00 of today**, whatever the hour — including
a late-evening run, which therefore plans from 08:00 that (past) morning. That is
precisely the pre-2026-08-03 behaviour, chosen knowingly.

## Behaviour

| When Optimize / a plan runs | Today | After this change |
|---|---|---|
| 09:30 Tue | plan starts 10:00 Tue | plan starts **08:00 Tue** |
| 10:30 Tue | plan starts 11:00 Tue | plan starts **08:00 Tue** |
| 23:00 Tue | plan starts 00:00 Wed | plan starts **08:00 Tue** (15 h in the past) |
| Fixed date in Settings (testing) | 08:00 of that date | unchanged — 08:00 of that date |

## Design

One module-level switch in `api/main.py`, mirroring the `COMMITMENT_FEATURE_ENABLED`
idiom already in that file:

```python
PLAN_START_NEXT_HOUR = False
```

Two call sites read it:

1. **`_resolve_config`** — auto mode (`plan_start_date is None`). Flag off: resolve
   to `plan_start_date=today (IST)` with `plan_start_floor=None`, so the engine's
   `max(08:00-of-date, floor)` degenerates to 08:00. Flag on: the stored-plan-clock
   path is entered unchanged.
2. **`_finalize_optimize`** — `_stamp_plan_clock()` is called only when the flag is
   on. With it off there is no clock to advance, so stamping would write a store key
   nothing reads.

**Nothing else changes.** `_ceil_next_hour`, `_stamp_plan_clock`,
`book_store.save/load_plan_start_floor`, `Config.plan_start_floor` and
`new_engine._plan_config`'s `max(08:00, floor)` all stay live and tested — they are
what the flag turns back on. Flipping to `True` restores today's behaviour with one
line, no migration, no schema change. Deleting them would touch the engine,
`_inputs_signature` and 13 tests for no benefit.

## Why this does not undo the 2026-08-07 "one plan, one set of dates" fix

That fix existed because the floor was recomputed per call, so two features planning
an hour apart were different plans (the live 07-Sep vs 04-Sep bug). Removing the
floor does not reintroduce it — it removes the varying input entirely:

- **Stability improves.** `08:00-of-today` is constant for the whole IST day. The
  stored clock it replaces advanced every time a contest finished; this does not
  advance at all until the date rolls.
- **Plan cache holds better** — the resolved config stops changing mid-day, so
  `_plan_fingerprint` is stable across the day.
- **No false staleness banner** — `_inputs_signature` already discards
  `plan_start_floor` (`api/main.py:375`), so an applied optimization does not begin
  reporting `inputs_changed`.
- **Golden trace and every fixed-date test untouched** — a fixed `plan_start_date`
  never carried a floor.
- **The `6eda289` rotation-anchor hazard disappears by construction.** That commit
  existed because a floor could roll `plan_start.date()` past `plan_start_date`
  (a Thu 23:xx run rolling to Friday, inverting the shift rotation). With no floor
  the two are the same date again. (Shift rotation has been a no-op since
  2026-08-05 regardless.)

## Known consequence, accepted

An Optimize pressed after the shifts end plans from 08:00 that morning, so the first
hours of the schedule are hours that have already passed. The owner accepts this: the
value of starting at the shift boundary when the book needs the full day outweighs a
late-evening run's stale first day, which the next morning's punches and the freeze
pass correct.

## Testing

- **RED first:** auto mode carries no floor; the engine plans from 08:00 for a
  non-08:00 "now"; a finished optimization no longer moves the plan clock; the plan
  start is identical whether resolved at 09:30 or at 23:00 on the same day.
- **The 13 existing next-hour tests stay** (`tests/test_plan_start_next_hour.py`,
  and the plan-clock tests in `tests/test_plan_consistency.py`), monkeypatching
  `PLAN_START_NEXT_HOUR = True` the way the commitment tests monkeypatch their flag.
  The mechanism keeps full coverage for the day it is switched back on.
- Full `pytest` green.
- **Verified on the real book, not only in tests:** a throwaway local instance on
  Test9, planning at a non-08:00 hour, confirming every surface reports an 08:00
  start and that Orders / Gantt / delay-report completion dates still agree
  order-for-order.
