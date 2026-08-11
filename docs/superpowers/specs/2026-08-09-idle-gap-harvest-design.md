# Idle-gap harvest — design + the blocker found before coding

**Status: DESIGNED, NOT BUILT.** Owner-approved to build (2026-08-09). Work stopped
at the integration blocker in §4, which must be done first.

## 1. The requirement (owner, verbatim in spirit)

> A machine that is idle, with a free qualified operator, and work pending, is not
> acceptable. Recover every hour of that.

Free operator + busy machine: fine. Busy machine + free operator: fine.
**Free machine + free operator + pending work: never acceptable.**

Explicitly out of scope: operator assignment / cross-training (not the owner's call),
and the ~9,000 h of machine idle that has no ready work at all (a mix/loading problem,
not a scheduling one).

## 2. The prize, measured (`scratchpad/ceiling.py`, re-runnable)

Physical rules respected: working windows only, predecessor finished, routing-eligible
machine, a qualified operator free AND booked, whole pieces, CNC/VMC pay setup per
engagement, each job's work is a pool consumed once. No delivery guard — it is a ceiling.

| Book | Recoverable | of which setup | **Actual extra cutting** | Engagements |
|---|---|---|---|---|
| Test9 wip=30 | 158.2 h | 36.0 h | **122.2 h (5.1 d)** | 53 |
| Test9 wip=68 | 101.0 h | — | ~4 d | 47 |
| Test8 wip=30 | 281.7 h | 48.0 h | **233.7 h (9.7 d)** | 67 |

73% of the recovery is on CNC/VMC **even after paying setup** — the long jobs and big
holes are there. Setup overhead is only 17–23%.

## 3. The design — harvest, do not re-schedule

**Every earlier attempt failed because it re-ran the dispatcher.** Change one placement
and the greedy cascade reshuffles, other orders lose their machines, deliveries slip.
Measured: first-fit backfill inside the GT selection cost the owner ~40 late-days on the
live book (a low-priority op that fit a small gap got the earliest end, became `crit`,
and won the dispatch — priority inversion propagating downstream). Reverted in `7d0ed71`.

So: **a post-pass over the FINISHED schedule that treats everything already placed as
frozen.**

1. Find windows where the machine is idle, a qualified operator is free, and work is
   pending whose previous routing step has already finished.
2. Move **part** of that pending job — as many whole pieces as fit — into the window,
   on that machine, with that operator.
3. Shrink the job's later block by exactly the pieces now done early (trim from its END,
   so its start never moves).
4. Nothing else moves. Repeat until no window qualifies.

### Why it cannot cost a delivery — by construction, not by testing

The only two effects are: some pieces are made **earlier** in time that was going to be
wasted, and the job's remaining block gets **shorter**, so it ends at the same time or
sooner. There is no mechanism that moves anything later. An order's completion can only
improve or stay equal. This is the guarantee the re-scheduling designs could not give.

Routing safety: harvest only work whose predecessor has already ENDED, so
`new_engine.routing_order_violations` stays at zero by construction.

### Where it lives

`ppc_engine/scheduler/` as a pure function over the finished `Schedule`, called at the
end of `decode()`. Everything — the plan, every optimizer candidate, the contest, the
cloud worker — goes through `decode`, so there is exactly one definition and no surface
can disagree. Cost is one pass over a finished plan; the deep search is not slowed.

## 4. ✅ THE BLOCKER — RESOLVED in `b1256f2` (step 1 shipped)

`engine/new_engine._entries_from_schedule` now emits **one entry per continuous
block**. A break caused by night / weekly off / shift change does NOT split an entry;
only the machine running ANOTHER job in between does. Each block publishes its own
start, end, qty, occupancy and operator segments.

Verified as a no-op on the current engine: Test9 wip=30 gave **410 entries before, 410
after, sha `8090ccb4` both** — byte-identical. Cross-surface audit clean (0 routing
violations on all seven surfaces, 0 of 68 date disagreements). Suite 847 passed. No
`SCHEDULER_FINGERPRINT` bump: presentation changed, placement did not.
Tests: `tests/test_entry_blocks.py`.

**Step 2 is therefore unobstructed.** Start here:

### Step 2 — the harvest, concretely

New pure function, `ppc_engine/scheduler/gap_harvest.py`:
`harvest(schedule, masters, config) -> Schedule`, called at the END of
`flow_scheduler.decode()` (so plan, contest, search and cloud worker all share it).

Inputs it must derive from the finished schedule only:
* machine busy intervals — from segments with `machine_id is not None`
* operator bookings — one interval list per operator name across ALL segments
* per `(order_key, op_seq)`: machine, total qty, earliest start, cycle_min
  (`masters.routings[item].operations`), and the predecessor's END (max end over the
  order's earlier routing positions — OS steps included)
* working windows — `worktime.iter_windows(machine, from, masters.calendar, config)`

Loop, gaps oldest-first:
1. Gap = machine idle inside a working window.
2. Operator = first in `masters.operators` qualified for the machine, whose
   `effective_shift` matches the window, available in the calendar, and not already
   booked in that interval (mirror `_lay_frozen`'s checks — do not invent a new rule).
3. Candidate = an op on that machine whose **predecessor END <= gap start**, whose
   current start is AFTER the gap, and which still has un-harvested pieces.
4. `setup = config.setup_min if op.kind is MACHINING else 0`;
   `pieces = int((gap_minutes - setup) // cycle_min)`; require `pieces >= 1` and a
   minimum useful run (30 min measured) so a hole is never spent on setup alone.
5. Emit new Segment(s) for `pieces` in the gap; **trim the SAME number of pieces from
   the END of that op's later block** (start never moves, so its end only comes
   earlier); update both blocks' `qty`.
6. Book the operator and the machine so the next gap sees them busy.
7. Recompute `Schedule.completion` per order as max end over its segments.

Then bump `new_engine.SCHEDULER_FINGERPRINT` (placement changes).

### The assertion that makes it safe

Not a general "score did not worsen" — assert **per order** that
`completion_after <= completion_before`. The design guarantees it (nothing moves later,
blocks only shrink); the test proves it on Test5/8/9 at wip 0/30/68. If any order moves
later, the harvest has a bug — do not paper over it with a score check.

Also assert: no machine double-booked, no operator double-booked,
`routing_order_violations == 0`, and mutation-test each part (this fixture family
passes vacuously — see CLAUDE.md).

## 4b. Original blocker description (kept for context)

`engine/new_engine._entries_from_schedule` builds **one ScheduleEntry per
`(order_key, op_seq)`**, with `start = min(segments)` and `end = max(segments)`.

A harvested job is the SAME `(order_key, op_seq)` in two blocks days apart. It would
collapse into one entry spanning the whole period, which:

* draws a Gantt bar **across the gap we just freed** — the machine looks busy for
  exactly the time we recovered, the opposite of the point;
* makes a split op's span swallow its successor ⇒ **false `ROUTING_ORDER_VIOLATION`s**;
* over-counts RUNNING hours in the delay report and machine-wise views;
* publishes `qty=float(first.qty)` — the wrong quantity for both blocks.

**Required first: one entry per CONTINUOUS BLOCK, not per operation.** Touches
`_entries_from_schedule` and every consumer that assumes one entry per step:
the Gantt, Schedule tab, shift-wise export, machine-wise view, delay report, analytics,
`optimizer.expected_completion`, and `freeze.schedule_projection` /
`freeze.compute_frozen_set` (both key off `process_seq`).

Do that as its own change, prove the cross-surface audit still reads 0 violations and
0 date disagreements, ship it, and only then add the harvest.

## 5. Acceptance criteria

* Idle hours (machine free + operator free + work pending) → **0**, or every remainder
  explained on screen with machine, window and reason.
* **No order's expected completion later than before the harvest** — assert per order,
  not in aggregate.
* Routing violations remain 0 on Test5/8/9 at wip 0/30/68.
* No machine or operator double-booked (whole-plan invariant test).
* Mutation-test every part: this fixture family passes vacuously by default
  (see `mutation-test` note in CLAUDE.md).
* Bump `new_engine.SCHEDULER_FINGERPRINT` — it changes placement.

## 6. Owner decisions already taken

* Splitting a job across two slots on the **same machine** is acceptable; the operator
  is whoever covers that machine on that shift (unchanged rule).
* Whole pieces only.
* Worst-order regression from the routing fix: **leave it**.
* Planning time may grow 2–3× if needed (this design barely costs anything, so unused).

**Still to confirm:** whether supervisors accept a job appearing twice on one machine in
the shift-wise sheet. If not, cap at one split per job.
