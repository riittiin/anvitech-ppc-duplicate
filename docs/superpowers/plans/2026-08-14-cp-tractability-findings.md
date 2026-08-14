# CP scheduler — tractability findings

**Measured 2026-08-14.** Harness: `scripts/cp_tractability_spike.py` (read-only,
plans nothing, writes nothing to any store). Every number below is re-runnable
with the command printed beside it.

This document answers four questions, in the order they matter:

1. Can the owner's book be solved at all, and at what budget?
2. E2 (hold across an unmanned shift, Rule 2 exactly) or E1 (forbid it)? If E1,
   what does its restriction cost in real operations?
3. Setup: `credit` (Rule 4 as written) or `always`?
4. Is the provably-dead head release bound worth removing?

---

## 0. What was measured, and what it is not

**The book.** No `MONGODB_URI` was available here, so every number is on the
repo's own generated workbook at the owner's SCALE:
`load_demo_book(n_items=58, n_orders=68)` → **68 SO lines, 61 batches, 362
tasks**. The owner's real book is ~68 SO lines / ~58 batches, so the *shape* is
right: same order count, same routing depth (~6 steps/item), same two-shift
calendar with a Thursday weekly off.

**It is a proxy, not his data.** What it licenses and what it does not:

| | |
| --- | --- |
| **Transfers** | model SIZE (variables/constraints scale with tasks × shifts × machine options, all of which are matched), and the RATIO between encodings |
| **Transfers, with care** | solve BEHAVIOUR — status and how the optimality gap moves with budget |
| **Does NOT transfer** | the absolute late-day numbers. 454 here is not 454 there |

**Two ways the proxy is EASIER than the real shop**, both of which mean the real
book is at least as hard, never easier:

* the generated fleet is **14 machines / 19 operators**; the real shop has ~26
  machines. More machines means more modes per task and more (machine, shift)
  roster pairs.
* the generated workbook's delivery dates are loose enough that the whole book
  finishes **on time**. A zero-tardiness book is the easy case for this engine
  twice over — phase 1's objective hits its own lower bound the moment any
  feasible schedule is found, and phase 2 is skipped outright. The owner's book
  is nothing like that (Test9 carries ~1,062 late-days). **So every headline
  number below is on `--due-shift 10`**: every delivery date pulled 10 days
  earlier, in memory, which makes the book genuinely tardy. The loose-book
  results are kept in §5 because the CONTRAST is itself a finding.

**Horizon 70 days, 120 shifts, 2 workers** unless stated. Horizon matters a
great deal to E2 and hardly at all to E1 — see §2.

**Model sizes are PHASE-1 sizes.** `solve_book`'s `stats["variables"]` /
`["constraints"]` are read off the proto after phase 1's objective is posted and
before phase 2 exists. Phase 2 adds **~60 constraints per dated order**
(measured: 24,478 → 28,139 at 61 batches, i.e. +3,661 over 61 jobs). Where a
phase 2 ran, `stats["phase_two_constraints"]` is the post-phase-2 total and is
quoted as such.

**E2 is being measured at its WORST CASE, deliberately and unavoidably.**
`rules._shifts_in_window` is the tightening E2's size rests on, and today it
only bites where a task carries a real time window — i.e. frozen in-progress
work, which Task 9 has not landed. On a clean book it returns the whole horizon
for every task, so **every E2 number below is the ceiling, not the steady
state.** A later task that gives tasks real windows will shrink E2. That is why
§2 also reports the horizon sweep: it is the cheapest available proxy for how
much a tightening would buy.

---

## 1. Can the owner's book be solved at all?

**Not to proven optimality — not at 5 minutes, not at 30. But a feasible plan
WITH A PROVEN LOWER BOUND is reachable at full scale under E1, and that is a
materially different thing from what the shop has today.**

E1 + credit, 61 batches, tardy book, 2 workers:

| budget | status | late-days found | proven bound | gap |
| ---: | :--- | ---: | ---: | ---: |
| 300 s | FEASIBLE | 454 | 168 | 2.70× |
| 600 s | FEASIBLE | 443 | 169 | 2.62× |
| 1800 s | FEASIBLE | 409 | 170 | 2.41× |
| 1800 s, **4 workers** | FEASIBLE | 428 | **215** | **1.99×** |

```
./.venv/bin/python scripts/cp_tractability_spike.py --mode scaling --e1 \
    --items 58 --orders 68 --horizon-days 70 --due-shift 10 \
    --time-limit 1800 --workers 2 --scale 61
```

Read this honestly, in both directions.

**The bad half.** Six times the budget bought a 10% better incumbent
(454 → 409) and moved the proven bound by **two days** (168 → 170). The bound is
where the "proven optimal" claim would have to come from, and it is not moving.
Nothing in this curve suggests that 2 hours, or 8, closes a 2.4× gap. **Anyone
planning to publish "CP-proven optimal" for the owner's book should stop.**

**The good half.** Every one of those runs returned a complete, rule-legal
schedule for all 61 batches, plus a number the shop has never had before: *no
schedule of this book can do better than 170 late-days.* The incumbent greedy
engine produces a plan with no bound at all. So the deliverable is not proof of
optimality — it is **a plan plus a floor**, and a search that can be given more
time when the owner wants it.

**CORES BUY THE BOUND, TIME BUYS THE PLAN.** The last row is the most useful
line in this document. Doubling the workers 2 → 4 at the same 30 minutes moved
the proven floor **170 → 215 (+26%)** while the incumbent got slightly *worse*
(409 → 428) — CP-SAT spends extra workers on proof strategies, not on the
incumbent. The gap therefore falls from 2.41× to **1.99×**. Both worker counts
were measured on this 8-core box with the 4-worker run sharing it with one other
solve, so if anything it is understated. **A worker with more cores is the
cheapest single improvement available**, and it improves precisely the half —
the bound — that six times the wall clock could not move at all. The Oracle
free-tier VM (4 OCPU) and a GitHub Actions runner (4 cores) both clear this bar
today.

**Where proof stops.** E1 + credit, tardy, 300 s each:

| batches | tasks | vars | constraints | status | late-days | bound |
| ---: | ---: | ---: | ---: | :--- | ---: | ---: |
| 10 | 55 | 4,138 | 7,715 | **OPTIMAL** | 43 | 43 |
| 20 | 116 | 5,767 | 11,235 | FEASIBLE | 77 | 56 |
| 30 | 176 | 7,287 | 14,465 | FEASIBLE | 103 | 68 |
| 40 | 236 | 8,807 | 17,695 | FEASIBLE | 174 | 91 |
| 50 | 296 | 10,327 | 20,925 | FEASIBLE | 276 | 122 |
| 61 | 362 | 11,999 | 24,478 | FEASIBLE | 440 | 168 |

**Proven optimality survives to ~10 batches and no further.** It does not
degrade gracefully to 20 — at 20 batches the gap is already 1.4× after five
minutes. Task 6's incidental reading ("12 OPTIMAL in ~10 s, 30 in ~19 s") was
taken on the **loose** book, where the objective bottoms out at zero; on a
genuinely tardy book that reading does not hold, and this table replaces it.

**Verdict on question 1.** *Ship it as a time-boxed FEASIBLE-with-a-bound
engine, or do not ship it.* The model runs to full scale and returns a usable,
bounded plan in minutes; it will not return a proof. If the plan's value was
premised on "provably optimal", that premise is false at this scale and this is
the moment to say so.

---

## 2. E2 or E1?

**E1. Not close, and not a judgement call — E2 returns nothing at all at the
owner's scale.**

Four variants, 61 batches, tardy book, 300 s each, 2 workers:

| variant | vars | constraints | status | late-days | bound | wall |
| :--- | ---: | ---: | :--- | ---: | ---: | ---: |
| E1 + credit | 11,999 | 24,478 | FEASIBLE | 454 | 168 | 302 s |
| E2 + credit | 136,439 | 207,119 | **UNKNOWN** | — | — | 198 s |
| E1 + always | 11,759 | 23,806 | FEASIBLE | 454 | 169 | 303 s |
| E2 + always | 136,199 | 206,447 | **UNKNOWN** | — | — | 196 s |

```
./.venv/bin/python scripts/cp_tractability_spike.py --mode variants \
    --items 58 --orders 68 --horizon-days 70 --due-shift 10 \
    --time-limit 300 --workers 2
```

**UNKNOWN means no solution was found at all** — not a worse plan, not an
unproven plan, nothing to publish. E2 is **11× the variables and 8.5× the
constraints** of E1 for the same book.

Where E2 stops (E2 + credit, tardy, 300 s each):

| batches | vars | constraints | status | late-days | bound | E1's answer |
| ---: | ---: | ---: | :--- | ---: | ---: | :--- |
| 10 | 24,538 | 36,948 | OPTIMAL | 43 | 43 | OPTIMAL 43 (in 10 s vs E2's 160 s) |
| 20 | 46,567 | 70,548 | FEASIBLE | 93 | 56 | FEASIBLE **77** |
| 30 | 68,487 | 103,858 | **UNKNOWN** | — | — | FEASIBLE 103 |
| 40 | 90,407 | 137,168 | **UNKNOWN** | — | — | FEASIBLE 174 |
| 50 | 112,327 | 170,478 | **UNKNOWN** | — | — | FEASIBLE 276 |
| 61 | 136,439 | 207,119 | **UNKNOWN** | — | — | FEASIBLE 440 |

E2 dies at **30 batches**, half the owner's book. Even where it survives it is
dominated: at 10 batches it reaches the same proven optimum **16× slower**, and
at 20 it returns a plan 21% worse (93 vs 77) for the identical budget. There is
no budget at which E2 wins on this data.

**E2 is not being judged on a short budget.** Re-run at 61 batches with a
1800 s total (1,080 s of it in phase 1, six times the budget of the table
above): **still UNKNOWN.** Nothing at all after eighteen minutes.

**Horizon sensitivity** (size only, 1-second solves, 61 batches). This is the
lever E2's size actually turns on, and it shows how much a later tightening
could buy:

| horizon | shifts | E1 vars / constr | E2 vars / constr | E2 : E1 |
| ---: | ---: | ---: | ---: | ---: |
| 45 d | ~78 | 11,053 / 22,916 | 89,865 / 138,765 | 8.1× / 6.1× |
| 70 d | 120 | 11,999 / 24,478 | 136,439 / 207,119 | 11.4× / 8.5× |
| 120 d | ~206 | 13,787 / 27,470 | 227,409 / 340,659 | 16.5× / 12.4× |

E2 grows **linearly in the number of shifts**; E1 is almost flat. Even a 45-day
horizon — barely enough for a book whose own best plan runs 170+ late-days —
leaves E2 at 90k variables, above the ~46k where it was already failing. **A
horizon squeeze does not rescue E2.**

### What E1's restriction actually costs

E1 forbids an operation from *overlapping* a shift in which its machine is
rostered but unstaffed. That is a restriction beyond the shop's four rules, so
it needs a number rather than a shrug. Measured by solving under **E2** — the
encoding that permits it — and counting the operations that actually do it:

```
./.venv/bin/python scripts/cp_tractability_spike.py --mode span \
    --items 58 --orders 68 --horizon-days 70 --due-shift 10 \
    --limit 20 --time-limit 600 --workers 2
```

At 20 batches (the largest scale where E2 returns anything), 600 s, FEASIBLE:

| | count | of 116 placed operations |
| :--- | ---: | ---: |
| overlapping an UNSTAFFED rostered shift — **what E1 forbids** | **3** | 2.6% |
| fully containing one — a part genuinely held in the chuck | **1** | 0.9% |
| overlapping a calendar break (off day / a single-shift station's night) | 5 | legal under BOTH encodings; not E1's cost |

**Given the choice, the solver held a part across an unmanned shift once in 116
operations.** E1 forbids something the search barely wants. The single held
operation was `('B009', 4)` on VMC2 across shift 11.

**Verdict on question 2.** *Ship E1.* Its cost is measured at ~3 operations in
116 at the largest scale where the comparison can be made at all, and its
benefit is the difference between a plan and no plan.

**Two honest caveats on that number.** (a) It is measured at 20 batches, not 61,
because E2 cannot reach 61 — a book three times larger and three times more
congested may want to hold more often, and this measurement cannot rule that
out. (b) E1 errs toward **under-claiming** capacity: a plan it produces is
always runnable, which is the safe direction. The failure mode is a slightly
pessimistic plan, never one the floor cannot execute.

---

## 3. Setup: `credit` or `always`?

**`credit` — Rule 4 as written. It costs essentially nothing.**

At 61 batches, tardy, 300 s: `credit` 11,999 vars / 24,478 constraints /
454 late-days / bound 168; `always` 11,759 / 23,806 / 454 / 169. The credit
encoding costs **+240 variables and +672 constraints (2.7%)** — the 48
setup-free modes and their linking clauses — and the two produce the **same**
objective value at this budget.

On the loose book the gap goes the other way and is larger: `always` proved
OPTIMAL in 156 s while `credit` needed a 180 s phase 1 plus a 94 s phase 2 for
the same answer, so the credit modes genuinely do enlarge the search. But 2.7%
of a model that is not the binding constraint is not a reason to ship a rule the
shop does not have. `always` charges 90 minutes on every changeover including
same-part, same-side ones; that is capacity the shop really does have and would
be thrown away.

**Verdict on question 3.** *Ship `credit`.* It costs 2.7% of model size and no
measured objective, and `always` is not Rule 4.

---

## 4. Is the dead head release bound worth removing?

**Yes — but for clarity, not for tractability. It is not a lever.**

`rules.add_release` posts two bounds per overlapping step pair. `rules.py`'s own
docstring proves the tail bound dominates the head bound, so the head can never
bind. Priced by monkeypatching `add_release` with a tail-only twin (E1 + credit,
61 batches, tardy, 600 s each):

| | vars | constraints | status | late-days | bound |
| :--- | ---: | ---: | :--- | ---: | ---: |
| with head (today) | 11,999 | 24,478 | FEASIBLE | 443 | 169 |
| tail only | 11,999 | 24,177 | FEASIBLE | **431** | 168 |

```
./.venv/bin/python scripts/cp_tractability_spike.py --mode head --e1 \
    --items 58 --orders 68 --horizon-days 70 --due-shift 10 \
    --time-limit 600 --workers 2
```

Removing it saves **301 constraints — 1.2% of the model** — and the run without
it found a slightly better incumbent (431 vs 443) at the same bound. That 12
late-day difference is **within search noise for two FEASIBLE runs** and must
not be read as an improvement; the honest statement is that removing the head
bound **costs nothing measurable and saves 1.2%**.

`_setup_charged` — 28 lines of the subtlest model-reading in `rules.py` — exists
only to feed that dead constraint, and removing the bound removes its only
caller.

**Verdict on question 4.** *Remove it, on maintenance grounds.* 1.2% will not
change any decision in this document. The real argument is that 28 lines of
delicate code whose only consumer is provably inert is a trap for the next
person, and it can be restored from git the moment something narrows the
interval and makes the head bound live again.

---

## 5. The finding that nearly got missed

The first pass of this measurement was run on the generated book **as loaded**,
and it said E1 solves the owner's scale **OPTIMAL with 0 late-days in 288 s**.
That result is real and it is worthless: the workbook's delivery dates are loose
enough that the whole book finishes on time, so phase 1's objective hits its
lower bound as soon as any feasible schedule appears and phase 2 is skipped
entirely. It measured **feasibility** and would have reported it as
optimisation.

The same book with `--due-shift 10` — the only change — returns FEASIBLE with a
2.4× optimality gap that 30 minutes does not close.

**Rule: a tractability measurement on a book with zero total tardiness is
measuring the wrong problem.** Any future re-run of this spike must carry
`--due-shift`, or the owner's real book, or it will report good news that is not
about his shop.

---

## 6. What would have to change to do better

In descending order of expected value, if proven optimality (or a much tighter
gap) is ever required at 58 batches:

0. **Give the worker more cores.** Measured, not speculative: 2 → 4 workers at
   the same 30 minutes moved the proven floor 170 → 215 and cut the gap from
   2.41× to 1.99× (§1). It is a config change on a box that already exists, and
   it is the only measured lever here that improves the BOUND.
1. **Give tasks real time windows.** `_shifts_in_window` is already written to
   tighten by itself the moment `earliest_start` / `latest_end` narrow. Task 9's
   frozen set does that for in-progress work; a release/deadline propagation
   pass would do it for everything. This is the only change that helps E2, and
   it helps E1's search too.
2. **Decompose.** Solve in time buckets (a rolling 2-3 week window at full
   fidelity, the tail approximated) or by machine group. The 10-batch result
   (OPTIMAL in 10 s) says small sub-problems are trivially solvable; the whole
   difficulty is scale.
3. **Warm-start phase 1 from the greedy engine's plan.** Phase 2 already warm
   starts from phase 1 and it works. Phase 1 currently starts cold, so 180 s of
   its budget goes into finding *any* good plan — work the roster engine has
   already done in milliseconds. This is the cheapest remaining improvement to
   the incumbent, and it does nothing for the bound.
4. **Accept the gap and spend the budget elsewhere.** 409 late-days with a
   proven floor of 170, in 30 minutes, on a worker that already exists, may
   simply be the product.

**Not worth trying, measured:** shortening the horizon to rescue E2 (§2 — even
45 days leaves E2 at 90k variables), and removing the head bound as a
performance measure (§4 — 1.2%).

---

## 7. Decisions, one line each

| # | Decision | Cost |
| --- | :--- | :--- |
| 1 | **Ship as a time-boxed FEASIBLE-with-a-bound engine**, ~15-30 min budget, **on a 4+ core worker**, at 61 batches | no proof of optimality: a 2.0-2.4× gap that 6× the wall clock does not close |
| 2 | **`cp_hold_across_unmanned_shift = False` (E1)** | forbids what the solver chose to do in 3 of 116 operations; measured at 20 batches, not 61 |
| 3 | **`setup_mode = "credit"`** | +2.7% model size, no measured objective difference |
| 4 | **Remove the head release bound and `_setup_charged`** | −1.2% model size; restore from git if anything ever narrows a task's interval |

**The provisional default recorded in the ledger before this measurement was E2.
This document reverses it.** `cp_hold_across_unmanned_shift` must default to
`False`, and Task 12's `Config` field must ship that way.
