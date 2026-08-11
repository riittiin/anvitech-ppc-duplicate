# Partial outsourcing returns — design

**Date:** 2026-08-07
**Status:** approved by the owner, not yet implemented
**Owner rule:** *"Since no arrival of the OS pieces is reported, the plan stays stable.
When 100 pieces arrive, we go on with CNC first side."*

---

## 1. The problem

A routing like:

```
1 BANDSAW OS   2 CNC FIRST SIDE   3 VMC FIRST SIDE   4 WASHING   5 PACKING   6 DISPATCH
```

Outsourcing does not return 400 pieces at once. The vendor sends ~100 at a time over
two or three days. The plan assumes the whole batch returns together, so CNC first side
is scheduled only after the **entire** outsourced block ends.

Verified on the live book (Test9): **34 of 34** outsourcing→next-step pairs start only
after the block ends. Not one starts early. The code reason is explicit —

```python
_INHOUSE = (MACHINING, MANUAL, INSPECTION)      # ppc_engine/scheduler/flow_scheduler.py
if config.overlap > 0 and just.kind in _INHOUSE and nxt.kind in _INHOUSE:
    ... release the successor early ...
else:
    ready_of[key] = paced_end                   # wait for the whole block
```

Outsourcing is excluded from the overlap rule, so the `else` branch always applies.

With a partial return already punched (125 of 500 on BANDSAW OS), a re-plan gives:

```
seq1 BANDSAW OS       qty 375   08-Aug -> 11-Aug      (it does know 125 came back)
seq2 CNC FIRST SIDE   qty 500   21-Aug -> 22-Aug      (still the full 500, still waiting)
```

The 125 pieces sitting in the building are not scheduled to move.

**The floor is not blocked** — the feedback precedence guard already accepts a punch of
up to 125 at CNC first side and refuses 175. So today reality runs ahead of the plan and
the plan catches up the next morning. The defect is that the plan misrepresents what can
be done, which costs trust and quotes pessimistic dates.

## 2. What this is worth — measured before designing

Ceiling measurement on Test9 (production config), shortening every outsourced lead time
as a proxy for earlier release:

| Scenario | Makespan | Late orders | Total late-days |
|---|---|---|---|
| Today — successor waits for the whole batch | 61.68 d | 61 | 967 |
| First tranche at 50% of lead | 63.54 d | 65 | 949 |
| Ceiling — successor could start immediately | 63.68 d | 62 | **936** |

29 of 58 orders pass through outsourcing, carrying 119 order-days of lead time — so the
exposure is real, but even an instant release improves total late-days by only ~3% and
makes makespan slightly worse. **The shop is crew-limited, not waiting-limited.**

**This work buys accuracy and trust, not speed.** That conclusion drives the whole
design: choose the smallest honest change, not the most capable one. The delivery lever
is crew coverage (CNC6 carries 584 waiting hours against 3 assigned operators).

## 3. The rule

1. **Nothing reported → the plan does not move.** The outsourced step keeps the lead
   time written in the Item's Process Master. No guessing at tranches, no date churn.
2. **Pieces reported arrived → the next step proceeds with exactly those pieces.** The
   rest stay at the vendor and move when they too are reported.
3. **The daily actuals are the only trigger.** Nothing else releases work early.

### Expected completion date semantics (owner, explicit)

The forecast always follows the Item's Process Master. If the master says 400 pieces
take 4,000 minutes, the plan says 4,000 minutes — unchanged from today. The quantity
still at the vendor is forecast to reach the next step after that lead time, exactly as
now. What changes is only that the **arrived** quantity is no longer forced to wait for
it.

```
seq1  BANDSAW OS       300   OS/Outsourced   08-Aug -> 11-Aug
seq2  CNC FIRST SIDE   100   CNC5            07-Aug 08:00 -> 07-Aug 16:00   <- run today
seq2  CNC FIRST SIDE   300   CNC5            11-Aug 08:00 -> 13-Aug 12:00   <- waits
seq3  CNC SECOND SIDE  400   CNC4            13-Aug -> 15-Aug
```

## 4. Scope — and what deliberately does NOT change

**Changes**

* Availability accounting: a pure function exposing "pieces available at this step now".
* The order object the engine sees carries that number.
* The scheduler lays **two placements** for the step directly after an outsourced step
  when some pieces are available and some are not.

**Explicitly unchanged.** The owner's constraint: *"I want to touch the minimum things
possible for this related change and nothing else."*

| Area | Why it needs no change |
|---|---|
| Optimizer, contest, apply, auto-trigger | Operates on sequences and settings, not on op placement |
| Gantt, Analytics, machine-wise, shift-wise, Schedule, delay report | All are views of the one schedule and inherit automatically — the property established 2026-08-07 |
| `book_signature` | **Verified:** already hashes `process_qty`, which is derived from the same punches, so an arrival already changes the signature and the auto trigger already fires |
| Cloud / Oracle contest payload | **Verified:** carries raw `orders` + `actuals` and rebuilds SO lines on the worker with the same pure function, so availability is recomputed identically — no payload field, no cloud/local divergence |
| Freeze / in-progress pinning | Frozen ops are pre-placed before the main loop and are untouched by this |
| Rules 1–3, consolidation, priority | Operate on batches, before any placement |
| Classic and flow engines | Retired; this touches the new engine's placement path only |

**Out of scope, documented so it is not a surprise later**

* **Late vendor.** If the planned return date passes with nothing reported, the
  outstanding quantity is re-planned from the current plan start with the **full** master
  lead time again. That is today's behaviour and this work does not change it.
* **Cascading the split.** Only the step directly after outsourcing splits (see §5).
* **Forecasting tranches.** The plan never predicts a delivery split it has not been told
  about.

## 5. Design

### 5.1 Availability — one number, one source

For an order and a routing step:

```
available_now(step) = good qty that cleared the PREVIOUS step
                    − good qty already recorded at THIS step
```

This is **the number the feedback precedence guard already computes** to decide whether
to accept a punch — the one behind *"Only 125 pieces have cleared the step before it,
'BANDSAW OS'"*. It is reused, not re-derived, so capture and planning can never disagree
about how many pieces are in the building. Lives in `engine/orderbook.py` beside
`precedence_cap_error`, pure, no I/O.

`available_now` is only meaningful where the previous step is OUTSOURCED. Elsewhere the
existing in-house overlap already handles piece flow, so the value is computed but unused.

### 5.2 Carrying it to the engine

`engine/new_engine.py` already maps the order book's per-process remaining onto the ppc
`Order` as `process_remaining`. The available-now map travels the same way, as a sibling
field. Absent or empty means "nothing has arrived early" and every downstream path
behaves exactly as today.

### 5.3 Two placements

In the scheduler's placement step, when laying operation *N* whose predecessor is
OUTSOURCED and `available_now(N) > 0` and `available_now(N) < remaining(N)`:

* **Placement A** — `available_now` pieces, ready from the plan start. "Ready" means
  eligible, not instant: it still waits for a permitted machine and a qualified operator
  on shift, exactly like any other operation.
* **Placement B** — the remainder, ready when the outsourced block ends (today's rule).

The two boundary cases need no special branch and must not get one:

* `available_now == 0` — nothing has arrived. One placement, ready after the outsourced
  block. Byte-identical to today.
* `available_now == remaining` — the whole batch has arrived and been punched. Then the
  outsourced step's own remaining quantity is zero, so it is already laid as a
  zero-duration milestone and the successor is already ready immediately. Existing
  behaviour, correct as-is.

Both are ordinary placements on the step's permitted machines and each charges its own
setup, because each is a real re-engagement of the machine. The operation's completion
for pacing purposes is the later of the two ends, so no downstream step can finish before
the last piece is through.

**At most two placements per step, always.** If four tranches arrive over four days, then
on any given re-plan the pieces are only ever in two states — available now, or still at
the vendor. The split does not proliferate.

### 5.4 Downstream re-merges

Only the step directly after the outsourced step splits. Once those pieces clear it they
are ordinary in-house WIP, and the existing 70% overlap already lets the next step start
before its predecessor finishes, with the piece-flow guard preventing it from finishing
early. Cascading the split would multiply every process line by the number of tranches
for no honesty the plan does not already provide.

## 6. Blast radius containment — proof obligations

The owner's stated concern is that changes break unrelated things. These are the tests
that must exist, not intentions:

1. **Dormant is byte-identical.** With no partial arrival anywhere, the plan for Test5,
   Test8 and Test9 is identical to before the change — same makespan, same late-days,
   same per-order completion dates. The golden trace is unchanged.
2. **Quantity is conserved.** The two placements always sum to the step's remaining
   quantity. No piece is scheduled twice, none is lost.
3. **Nothing finishes early.** No step's work finishes before the last piece it needs has
   cleared the step before it — the existing piece-flow invariant still holds.
4. **The cross-feature audit still passes.** All 18 checks (completion dates across
   Orders / Gantt / delay report / machine-wise / shift-wise, operator and end time per
   op, makespan, utilization, quantities, staffing) clean on Test5/8/9.
5. **Capture and planning agree.** The quantity the plan treats as available equals the
   quantity the precedence guard would accept at that step.
6. **Frozen work is undisturbed.** An in-progress operation stays pinned to its machine
   and operator when a tranche arrives elsewhere in the same order.
7. **Full suite green** (809 at time of writing) with no test modified to accommodate the
   change.

## 7. Risks

| Risk | Mitigation |
|---|---|
| The Gantt reads as cluttered for outsourced orders | The two bars can be collapsed visually without touching the plan; decide after seeing it on real data |
| Extra setups make an order look *later* than before | Real, not an artefact — surface it plainly; four tranches genuinely cost four setups |
| A change in the vendored `ppc_engine` breaks something unrelated | Obligation 1 above; the new field is inert when absent, so every existing caller keeps its exact behaviour |
| The owner expects delivery to improve | §2 states it will not; this buys truth, not speed |

## 8. Testing

* Unit: availability accounting, including the boundary where everything has arrived
  (no split) and where nothing has (no split).
* Unit: two placements sum to the remaining quantity; each carries its own setup.
* Integration: punch a partial arrival on a real book, re-plan, assert the arrived
  quantity is scheduled before the outsourced block ends and the remainder after it.
* Regression: obligations 1–7 of §6.
