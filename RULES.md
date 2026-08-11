# Anvitech PPC Engine — Rules in Execution Sequence

> Source of truth for the Production Planning & Control engine.
> Rules are reordered from the `PPC logics` sheet of the source workbook into the
> sequence in which the engine actually executes them. Each rule maps back to
> its original number for traceability.

## How to read this file

Each rule is tagged by its role in the data flow:

- 🔻 **Pipeline stage** — a true `output → input` handoff. Its output is the
  next pipeline stage's input.
- ⚙️ **Parameter / calc** — not a transform. A rule/setting that is *consumed
  during allocation* (Rule 6). It sits beside the pipeline, not in it.
- 🔁 **Loop** — flows back to an earlier stage instead of forward.

### Data flow at a glance

```
   Raw SO lines
       │
   🔻 Rule 1  Consolidate
       │  batches {item, qty, SO delivery date}
   🔻 Rule 2  Sort by delivery date
       │  ordered batches
   🔻 Rule 3  Smart priority (least slack)          ←─ also reads routing (Item's process Master)
       │  prioritized batch list
       ▼
   🔻 Rule 6  Allocate to machines  ◄── ⚙️ Rule 4 (setup time)
       │                            ◄── ⚙️ Rule 5 (overlap mode)
       │                            ◄── masters (machine / operator / calendar)
       │  the schedule
   🔻 Rule 7  Capture daily actuals
       │  actuals
   🔁 Rule 8  Re-run MRP  ──────────────────────────► back to Rule 1
```

**True forward pipeline:** `1 → 2 → 3 → 6 → 7 → 8 (loop)`
**Consumed inside Rule 6:** Rules 4, 5.

---

## PHASE 1 — Build the demand list *(what to plan)*

### 🔻 Rule 1 — Consolidate sales orders  *(Pipeline stage)*
Group SO lines of the **same item code** whose **delivery dates fall within a
10-day window** (user-configurable) into a single production batch.

- **Source:** original Rule 1
- **Input:** `Sales Order (SO) list` (raw SO lines)
- **Output:** list of batches `{item code, total qty, SO delivery date, source SO refs}`
  where the batch's **SO delivery date** is the earliest SO delivery date among the
  merged lines (the binding customer commitment). The SO Delivery Date column is the
  single, authoritative delivery date used for logic and display — no other date.
- **Parameter:** consolidation window = 10 days (configurable)

---

## PHASE 2 — Prioritize the batches *(order in which jobs claim machines)*

### 🔻 Rule 2 — Sort by delivery date (primary)  *(Pipeline stage)*
Order all batches by **earliest SO delivery date** first.

- **Source:** original Rule 2
- **Input:** batches from Rule 1
- **Output:** same batches, ordered by delivery date

### 🔻 Rule 3 — Smart priority: workload-aware urgency (secondary)  *(Pipeline stage)*
Strict earliest-due-date alone is naive: a 1-piece order due 19 Jun would outrank
a 100-piece order due 20 Jun, even though the big order is far more at risk of
being late. Rule 3 fixes this with an operations-management dispatching metric
that folds **due date and workload** into one urgency number:

- **Slack** = (working time until SO delivery date) − (work needed), where *work
  needed* = the batch's total machine occupancy (Rule 4: cycle×qty + setup,
  summed over the routing). **Least slack = most at-risk = highest priority.**
  (Alternative metrics: *critical ratio* = time ÷ work; *process time* = legacy.)

On **equal** SO delivery dates the available time is the same, so least-slack
reduces to "more work first" — preserving the documented same-date oracle
(`61241949-01` before `61247047-01`). On **different** dates it weighs the extra
time against the extra work, so a heavy order due slightly later can correctly
jump an earlier trivial one. A `priority_window_days` knob bounds how far apart
two dates may be for the metric to reorder them (default: no limit).

- **Source:** original Rule 3, generalized
- **Input:** ordered batches from Rule 2 **+ routing lookup** (`Item's process Master`,
  for work content) + config (metric, window)
- **Output:** fully prioritized batch list (+ a per-batch slack/critical-ratio
  breakdown shown on the Rule 3 tab)
- **Excel example:** `61240807-01` (highest process time) and `61247047-01`
  (lowest) rank as their Remarks specify; the new metric also handles
  near-date/large-quantity cases the old exact-tie rule missed.

---

## PHASE 3 — Allocation parameters *(consumed inside Rule 6, not pipeline stages)*

### ⚙️ Rule 4 — Setup time per process  *(Parameter / calc)*
Add **90 min setup time** on top of (cycle time × qty) when computing how long a
process occupies a machine — but **only for CNC/VMC machining** (the setup models the
time to program/set the machine). Manual/finishing steps (washing, deburring, packing,
inspection, drilling/chamfer, bandsaw, manual lathe) need no such setup and occupy
their station for run time alone. A machine is CNC/VMC by id (`CNC*`/`VMC*`) or by its
Machine-master type (CNC lathe / Vertical Machining center).

- **Source:** original Rule 5a
- **Consumed by:** Rule 6 (machine occupancy calculation); see `rule6_allocate._is_setup_machine`

### ⚙️ Rule 5 — Operation overlap mode  *(Parameter / calc — global toggle per plan run)*
Provide a two-option selection:
- **(a) Sequential** — a process starts only after the previous process is
  **fully completed**, OR
- **(b) Overlap** — a process starts after **50% (user-defined) of the previous
  process's _cutting time_** is done.

**What the 50% measures (data-confirmed decision).** Machine occupancy =
*cutting time* (cycle × qty) **+ a 90-min setup** (Rule 4, CNC/VMC steps only). The source rule says
"50% of first operation time," but does not define whether that includes setup.
It is measured against the **cutting time only — the setup is excluded**, because
while the previous operation cuts, the *next* machine's own setup runs in
parallel (so the previous setup need not be counted). This makes the knob mean
"start the next operation when X% of the parts are machined," and it shortens the
plan more than counting setup would.

**No-cutting steps don't overlap.** A finishing/inspection step with **no cycle
time** (deburring, CMM, inspection, washing, packing) produces nothing gradually,
so overlap is meaningless for it — its successor **waits for it to fully
complete** (as in sequential mode). Overlap only compresses real machining steps.

**A step can START early but cannot FINISH before its predecessor delivers the
last piece (pacing).** Overlap lets a step begin after 50% of the previous step's
cutting — but a step processes pieces only as fast as its predecessor hands them
over. A **fast** step after a **slow** one (e.g. INSPECTION on 3 stations after
WASHING on one) is *starved*: it starts early (pipelined) yet its completion is
**held to its predecessor's end**, finishing just after — never before. Without
this, a fast step would appear to finish before the slow step delivered its last
pieces, and those pieces would be dispatched having skipped it. So each step's END
is ≥ every earlier step's END (ends are monotonic through the routing); its span
grows (idle waiting for pieces) while its work — cutting × qty + setup — is
unchanged. The overlap *start* and the schedule's compression are preserved; only
the impossible early *finish* is corrected.

- **Source:** original Rule 6 (sheet rows 19–20, verbatim: "second operation
  shall start after first is fully completed" / "after 50% (or user defined) of
  first operation time is over")
- **Consumed by:** Rule 6 (start-time calculation for each next process)
- **Parameter:** overlap threshold = 50% (configurable). The percentage applies
  to the previous process's cutting time, not its setup.

> **Note — the old "parallel machine" rule was removed.** An earlier draft had a
> rule that, for batches over 400, moved a CNC operation to a separate machine.
> It never delivered true parallel processing (it relocated the operation rather
> than splitting the batch) and is no longer part of the engine. The remaining
> rules are renumbered into a clean 1–8 sequence.

---

## PHASE 4 — Allocate processes to machines *(the actual schedule)*

### 🔻 Rule 6 — Allocate to machines *(Pipeline stage — TWO engines, config-selected)*

**The flow scheduler (2026-07-19, LIVE mode after cutover)** — chosen by
`scheduler: "flow"` in the saved plan config. Built from the three basics only:
machine + qualified operator on every working minute; operators strictly within
their own shift; process order respected piece-wise. What it does differently:

- **No resource-holding.** A machine and its operator are occupied ONLY while
  cutting pieces that already exist at that step. A step starved of parts
  RELEASES the machine for other jobs and resumes when more parts arrive. (The
  classic engine seized resources at op start and held them to the paced end —
  measured cost on the real book: ~20 days.)
- **Chunked piece-flow.** A batch moves between steps in `qty / flow_chunks`
  transfer chunks (contest-tuned, 4 typical): the next operation starts once
  the first chunk has cleared its predecessor, and chunks of one step may run
  concurrently on alternative machines. No piece ever runs at step k+1 before
  it physically exists at step k (validator-enforced).
- **Setup per re-engagement.** The 90-min CNC/VMC setup is charged every time
  the machine takes up an op it wasn't just running; consecutive chunks of the
  same op pay once. More honest than one-per-step — a plan pays for every
  job-switch its packing causes.
- **Scarce-first crewing** (same rule as classic): among free qualified
  operators, spend the least-flexible person first.
- The Optimize contest tunes `flow_chunks` (the overlap % has no meaning
  without pacing); OS/DISPATCH/off-machine semantics unchanged; operator
  absences (`reserved=`) and the punched-quantity feedback loop (per-process
  remaining = initial WIP downstream) fully honored.

Measured on the live 71-order book (2026-07-19): classic optimized 70.8 d /
1460 late-days → flow unoptimized 49.6 d / 1431 → flow + search ~44 d / ~1170.
The certified crew capacity floor is 37.2 calendar days.

**The classic engine below** (`scheduler: "classic"`, the engine default) is
unchanged and remains the byte-identical historical behaviour.

### 🔻 Rule 6 — Assign each process to the earliest-available preferred machine  *(Pipeline stage)*
Allot each process to the **earliest-available machine from its
preferred/suggested list** — respecting:
- the working calendar (**Thursdays off**, holidays, operator leaves), and
- **shift timings** (1st: 8am–7pm, 2nd: 7pm–5am). **Operators & shifts live in the
  app, not Excel** (2026-07-18) — the admin adds, edits, removes, and sets each
  operator's shift in **Settings**, and that shift **holds every week until an
  admin changes it** (2026-08-05) — there is **no automatic rotation** and no
  "stays on current shift" pin; the planner always uses the shift on file. The
  workbook's "Operator & shift Master" sheet only ever **seeds** this table once,
  the first time it's empty — a later re-upload never touches operators again.

**Time basis.** A process's machine time = **cycle time × qty** (+ the 90-min setup
for CNC/VMC steps only, Rule 4). The Process "Total time" column in the master is **never** used.

**Operator staffing is per shift (2026-07-19).** An operation must have a **qualified
operator on every shift it runs in**. When a job crosses the 19:00 (or 05:00) shift
boundary, a fresh qualified operator on the new shift **takes over** (a handoff:
same machine, different person, chosen free-and-least-loaded). If **no** qualified
operator on the new shift is free, the **machine pauses** and the job resumes when
one frees up. A person is never billed outside their own shift and never runs two
machines at once. This replaced the earlier model that kept the starting operator
on the job across shift boundaries — that model quietly assumed ~900 hours of night
machining with nobody on the floor (the old "unstaffed" warning) and showed
completion dates the crew could not actually achieve. Plans are now honest: every
scheduled minute is staffed, and the schedule's dates reflect the real crew.

**Non-delay scheduling (keep machines running).** The engine schedules at the
*operation* level, not batch-by-batch: at every step it considers the next ready
operation of **every** batch and starts the one that can begin earliest on its
machine. A machine is **never left idle while any operation is ready for it** —
the instant a machine finishes one batch's operation, the next operation that
needs it (from whichever batch whose previous process is already done) takes it.
The priority from Rules 1–3 only decides between operations that are *equally*
ready; it never forces a machine to wait for a higher-priority batch whose
operation isn't ready yet. This is the core of the optimization: maximize machine
utilization while honouring delivery-date priority.

**Expedite window (`expedite_window_min`, default 0 = off).** Pure non-delay
(above) breaks ties only when two operations can start at the *exact* same instant;
when the earliest-startable op is a few minutes ahead, it wins even if a **more
urgent** order's op is right behind it — so an overdue order can keep losing every
near-race for a shared machine/operator and finish weeks late. The expedite window
softens this **without ever idling a resource**: among the operations that could
start within `expedite_window_min` minutes of the *earliest* startable one, the
scheduler picks the one with the **least slack** (dynamic slack = working time to
its SO delivery date − work it still needs), rather than the one that is merely a
few minutes earlier. With the window at **0** the behaviour is exactly the legacy
non-delay tie-break (byte-identical plans). A small window (≈45 min) pulls the
worst-stuck urgent orders forward by days while never pushing a comfortably on-time
order late — it only redistributes machine/operator time *among orders competing at
nearly the same moment*. It is a tie-break refinement, not a reordering: no machine
ever waits for a not-yet-ready op. *(2026-07-19: the Settings tick mark for this was
removed — measured consistently harmful under per-shift staffing, and it is forced
off whenever an optimized plan is applied. The config field remains, default 0.
The overlap % control left Settings the same day: the Optimize contest auto-tunes
it and Apply persists the winner, so users no longer set it by hand.)*

**Alternative ("preferred") machines.** A process's *Suggested M/c* cell may list
**alternatives separated by `/`** (e.g. `CNC3/CNC6` = run on either CNC3 or CNC6).
The scheduler picks the **earliest-available** of the allowed machines, so work
**load-balances** across them automatically (once one is taken it's busy, so the
next contending operation grabs the other). On a tie the **first-listed** machine
wins (it's the preferred one), keeping plans deterministic. A listed machine not
yet in the Machine master is registered as a provisional machine and still used.
This applies to **any** alternative cell — CNC, inspection (`MI1/MI2/MI3`), etc.

**Suggested vs Allotted, and what the parallelization toggle spans.** The *Suggested
M/c* cell lists every machine the item is **capable** of using; the *Allotted M/c* cell
is the machine(s) actually **allotted** for the step. Which set the scheduler may use
depends on the parallelization toggle (`split_parallel`):

- **Toggle OFF** → the step uses its **Allotted** machine(s) only (the planned choice).
  If the Allotted cell is blank, it falls back to the Suggested machine(s) so the step
  still schedules.
- **Toggle ON** → the step may use the **union of Allotted + Suggested** (Allotted
  first) — every capable machine — so work load-balances and (for batches over 400)
  splits across all of them.

So with the toggle off, an item with Allotted `CNC4` and Suggested `CNC3/CNC6` runs only
on `CNC4`; with it on, it may run on `CNC4`, `CNC3` and `CNC6`. Ties prefer the Allotted
machine (it is listed first). This is independent of OS/DISPATCH handling, which is
decided before machine selection.

**Parallel split (`split_parallel`, default on in the UI) — LARGE batches only.**
When a step lists alternatives, the engine **splits the quantity across the machines the
toggle exposes (Allotted only when off, Allotted+Suggested when on) to finish
the step as early as possible** — but **only for batches over 400 pieces**
(`split_min_qty = 401`). Each candidate gets the load it can complete by a common target
finish time, counted **from when it becomes free** — so a machine that's busy now but
frees soon still takes a (smaller) share, and a faster machine takes more, so both halves
finish together. It splits **only when that beats the single best machine**. The next
process waits for the slowest half (split-then-recombine). Same logic for any alternative cell.

A batch of **400 or fewer** is **not split** — splitting a small job would waste a second
90-min setup for little gain. Instead the whole batch runs on the **single least-queued
(earliest-free) alternative**, so small jobs still load-balance across the alternatives
(e.g. two 10-piece inspection lots land on MI1 and MI2 by whoever's free), just without
the extra setup.

**Off-machine steps — DISPATCH vs OS / outsourcing.** A process that runs off any
in-house machine is handled in one of two ways:

- **DISPATCH** (the final "consider it done / shipped" gate) and any other step with
  **no machine and no cycle time** → a **zero-duration milestone** on the
  "Off-machine" lane (or "OS / Outsourced" if its name has an `OS` word). It consumes
  no machine, operator or time. The dispatch gate is matched tolerantly — `DISPATCH`,
  `Dispatch`, and the real-data misspelling `DISAPTCH` all count.
  **DISPATCH waits for the WHOLE order.** Because the Rule 5 overlap can let a *later*
  fast step finish before an *earlier* long step (e.g. INSPECTION on 3 stations finishing
  before WASHING on one), the dispatch milestone is placed at the **latest end across all
  of the batch's processes**, not at its immediate predecessor's overlap point — so an
  order is "dispatched" only once **every piece has cleared every process**. (The overlap
  rule itself is unchanged for every real step; only the dispatch gate is held back.)
- **OS / outsourcing with a turnaround time** — a step marked `OS` in its Allotted (or
  Suggested) machine cell, carrying a **cycle-time value in minutes** (e.g. `7200`).
  This is scheduled as a **reserved continuous block**: it holds that many minutes of
  vendor turnaround **flat per batch** (NOT × qty, no 90-min setup), runs **continuous
  24×7** (it ignores Anvitech's Thursday-off and shift hours — the vendor works on its
  own clock), takes **no in-house machine or operator**, and has **unlimited parallel
  capacity** (any number of orders can be at OS at once). An OS step is **fully
  sequential on both sides**: its in-house **predecessor runs to 100% completion before
  the block starts** (no overlap *into* an OS step — you can't ship parts that aren't
  machined yet, so all of process 1 finishes whether it's 20 or 1000 pcs, regardless of
  the overlap toggle), and the next process **waits for the full block to finish** (no
  overlap *out of* it). Shown as an `OS` bar on the "OS / Outsourced"
  Gantt lane; kept out of the machine-utilization table (it is not a machine). If the
  cycle-time cell is **blank**, the step is a zero-duration milestone until a number is
  entered — then it reserves that block automatically, no code change. An OS step that
  arrives early is closed via Capture Actuals (its per-process remaining hits 0 and the
  next Plan skips it). A blank machine *with* a real cycle time is still NOT off-machine
  — it surfaces as "needs machine" so genuinely missing data fails loud.

**Operator & shift logic (`apply_operator_logic`, default on in the UI).** Each
machine's working window is driven by its **Available Hrs/Day** and by operator
coverage: a machine with Available Hrs/Day ≥ 12 (CNC/VMC ≈19.5) is a **two-shift**
machine (08:00–19:00 + 19:00–05:00); a smaller value (≈9.5 — saws, milling,
drilling, all manual stations) is a **single-shift** resource running **09:00–18:00
only**. An activity may run on a machine **only during a shift that has a qualified
operator** — an operator whose specialty (Preferred Machines, matched by machine
number *or* type, e.g. `CNC 1` or `Milling M/c`) includes that machine and whose
**Shift** (First/Second) covers it (manual work needs a **first-shift** operator).
So a machine runs the union of its operator-covered shifts: e.g. a CNC with only
second-shift operators runs second shift only. An operation whose machine has **no
covered shift** is **not scheduled** and is listed in a "needs operator" report
(never fatal); operator specialties that match no machine are reported too. The
machine/calendar masters are **Excel-driven** — edit the master sheets, re-upload,
and it reflects. **Operators and their shift are app-owned** (see above) — edit
them in **Settings**, not Excel; a re-upload never changes an operator's shift or
roster. Provisional machines (referenced by a routing but not yet in the master)
bypass the coverage gate and keep a two-shift window.

**Operators are one-at-a-time resources (load-balanced, scarce-first).** Each
scheduled operation is assigned a specific person. Among the qualified operators
who are **free** at that moment, the scheduler spends the **least-flexible person
first** — the one qualified on the fewest machines — so flexible people (who can
run VMCs *and* CNCs) stay available for the machines only they can run. Without
this, a flexible operator could get grabbed for work a specialist could do, and
the machine only the flexible person can run would sit paused waiting for them
(measured on the real book, 2026-07-19: this one rule cut the plan from 78.5 to
73.7 days with the identical job order). Ties (same flexibility) go to whoever
freed up earliest, then sheet order — deterministic. That person is then busy
until the op (or their shift segment of it) ends. So work **spreads across the
whole crew** instead of always landing on the first-listed name: with four interchangeable
helpers (HP1–HP4) the load divides four ways, and an operation **waits for a free person**
when all qualified operators are busy, capping concurrency at the real headcount (e.g. at
most 4 helper jobs at once, not 5). On a tie (everyone free) the first-listed operator
wins, keeping plans deterministic. Parallel-split siblings get **distinct** people (one
person can't run two machines at once). Inspectors (MI1/MI2/MI3, one per station) are
naturally balanced; CNC/VMC operators and helpers now share load. The assigned person
shows in the `Operator` column on the Rule 6 schedule (present only when operator logic is
on). With operator logic **off**, no person is assigned and concurrency is machine-limited
(unchanged).

**Continue from reality — per-process remaining (the feedback loop).** Every ordered
piece must pass through every process, so a piece has *cleared* a step once it is
punched as good there (Rule 7). When a re-plan runs, each process is scheduled for
its **own** remaining = **ordered − pieces already done at that step** (not the whole
order through every step). Earlier steps have more done, so they schedule fewer
pieces; the finished-goods gate (last/DISPATCH step) has the most. A step already
**fully** produced (remaining 0) is **skipped** — finished work is never re-run, and
its pieces are ready for the next step. So after a day's punching, the plan picks up
**where the floor actually is** instead of restarting the routing. (With no recorded
progress, every step runs the full order qty — today's behaviour, byte-identical.)
This is the order book's `process_qty` carried through Rule 1's consolidation into
Rule 6; the finished-goods gate is just this rule applied to the **last** process.

While running, Rule 6 consumes: ⚙️ Rule 4 (setup time) and ⚙️ Rule 5 (overlap mode).

**Balance operator workload (`balance_operator_load`, default off).** A
**schedule-neutral** fairness pass applied *after* the plan is built. The first-listed
tie-break can pile work on the early-named operators (e.g. one at 106% while an equally
qualified peer sits idle). When on, each already-scheduled operation is reassigned to
the **qualified, same-shift operator who is free at that moment and has the least work
so far** — spreading load evenly across interchangeable people. It **never changes any
start/end time**, so **makespan and lateness are provably unchanged** (only *who* runs
each job changes). It **never double-books a person**: after the fairness walk, a
repair pass reverts reassignments until no operator holds two overlapping ops (the
walk's "keep the original when nobody is free" could otherwise collide with the walk's
own earlier reassignments — found live 2026-07-15, one person on CNC1 + VMC3 at once;
regression: `tests/test_operator_invariants.py`). Measured on Test5: peak operator
106%→97%, spread (stdev) 23→16, with an identical schedule. See
`rule6_allocate._rebalance_operators`.

**Operator load can never exceed 100% (owner guarantee), and uncovered work is
surfaced as UNSTAFFED.** The shift-wise view **follows the plan first**: every
segment whose named (Gantt) operator covers that shift shows that same person — the
printed shift sheet and the Gantt never disagree (2026-07-15 audit fix). Segments the
named person physically can't work (the other shift of a multi-day block) go to a
qualified person who is actually **free**; when a shift runs more machines than it
has qualified people (e.g. three VMCs overnight with two night-shift VMC operators),
the extra segment is marked **`⚠ Unstaffed`** instead of being billed to an
already-busy person (that double-billing pushed operators to 107% live). The fairness
rebalance also treats people **reserved** (e.g. by an operator absence, above) as busy,
so it never reassigns work onto someone unavailable at that time (originally an
audit fix for the now-removed two-pass committed/open split, where a person could land
on two machines at once across the passes; the same protection now guards the single
pass's reservations).
Analytics rolls those segments into a headline **"unstaffed hours"** number with an
explanatory note — the plan itself is untouched (reporting only); the note tells the
owner where extra shift crew is genuinely needed.

- **Source:** original Rule 4 + `Machine master` + `Operator & shift Master` +
  `Weekly off & holiday master`
- **Input:** prioritized batch list (Rule 3) + Rules 4/5/7 + masters
- **Output:**
  - the schedule `{batch, process, machine, start, end}`, plus
  - a **machine-wise view** derived from it: each machine's ordered queue (with
    an *idle-before* column = working minutes the machine waited) and a
    **utilization** summary (busy / idle-within-span / utilization %) — this is
    how you see that machines run continuously.
  - Drives `Planning status monitoring`, `Machinewise`, `Weekly Production plan`.
  - an **Analytics view** (`engine/analytics.py`): utilization & bottlenecks from the
    plan — each machine/operator/process measured against **its own** available time in
    the plan window (busy ÷ available), so a single-shift manual station and a two-shift
    CNC are judged fairly. Flags bottlenecks (≥85%) and under-used resources (≤30%).

**Recorded times are NOT fed into the plan.** The downtime categories (No Power, No
Operator, Tool Problem, Breakdown, …) and the actual setup time are captured and
stored **for the record only** (shown in the Rule 7 rollup). They never affect the
schedule — the feedback loop is driven **purely by quantity produced/rejected per
process** (the director's spec). A power cut or over-long setup is logged for
analysis but does not move the plan.

### Order commitment (lanes) *(feature — TWO lanes; committed is soft-protected, 2026-07-29)*

Every order occupies exactly one **commitment lane**, shown as a badge on the Orders tab:
- **Open** (default) — new or not yet promised. A pure status label — no scheduling
  effect at all.
- **Committed** — the admin has told a client this order is promised; snapshots its
  **current expected completion date** as `promised_date`. Committed orders now DO
  have a scheduling effect: the **promise cap** below.

**Urgent is removed (2026-07-29).** It was the more-protected lane (its `promised_date`
came from the SO delivery date instead of the current plan); the button, badge, and
`POST /orders/urgent` endpoint are gone. A stored order with `commitment == "urgent"`
migrates to `"committed"` on load (its `promised_date` is kept unchanged) — committed
is the safe mapping since Urgent was the more-protected of the two. The Orders tab now
shows **Committed** and **Open** only.

`promised_date` is kept for **display** on Committed orders: the Orders tab shows
Promised vs Current-expected side by side, with a **red drift flag** when the plan's
current expected date has slipped past promise + slack — so the owner knows which
customers to call. The admin may **Commit** or **Uncommit** (returns to Open) an order
at any time.

**An all-open book (nothing committed) plans byte-identical to before this feature** —
a regression, not an aspiration (`tests/test_replay_single_pass.py`).

> **Historical note (superseded 2026-07-16, then partially reversed 2026-07-29):** an
> earlier design (2026-07-13/14) made lanes protective — a two-pass scheduler ran
> Committed+Urgent orders first and reserved their machine/operator time so Open orders
> could never push a promise late, plus an automatic "promise recovery" re-sequencer for
> disrupted committed orders. A follow-on design added a hard promise **veto** (any
> candidate plan breaking a promise scored infinite). Measured on both real books, the
> veto approach scored **~30% worse** than the simple two-pass shape — zero-slack
> promises collapse the feasible search region. The owner then redefined the model to a
> purely informational one (2026-07-16): lanes were pure labels, no scheduling effect
> at all, and all of that machinery (two-pass, the veto, promise recovery, the urgent
> push-warning preview) was removed. **That informational-only model is itself now
> partially reversed** by the committed-promise cap below: committed orders regained a
> real, but *soft* (convex-penalty + no-regression backstop, never a hard veto or
> reserved capacity), scheduling effect — see the cap's own note on why this doesn't
> repeat the July collapse. Open orders remain exactly as informational as before.

### Committed-promise cap — soft +3-day ceiling *(feature, 2026-07-29,
`docs/superpowers/specs/2026-07-29-committed-date-stability-design.md`)*

The owner's rule: on a rolling order book, re-optimizing to fit in new (Open) orders
must never push an already-**committed** order's expected completion more than a
few days past what the customer was promised. A committed order's expected date **may
move earlier by any amount**; it must **not slip later** than `promised_date +
committed_promise_slack_days` (a Config knob, default **3 days**). Open orders absorb
the slack instead — they wait, not the customer's promise.

This reuses the proven **2026-07-24 worst-order-ceiling** pattern rather than the
2026-07-13/14 hard two-pass/veto shape that collapsed the feasible region (~30% worse,
see the historical note above) — soft in the search, hard only at the moment a plan is
adopted:

- **Soft, in-search.** A convex penalty term — `COMMITTED_PROMISE_WEIGHT ×
  committed_promise_breach`, where `committed_promise_breach` = Σ over committed
  orders of `max(0, expected_completion_days − promise_ceiling_days)²` — is added to
  the score in **both** search engines (`engine/optimizer.score` and the `ppc_engine`
  objective mirror). High enough (measured on Test8) that the optimizer keeps
  committed orders inside +3 whenever feasible by pushing **Open** orders later
  instead — "committed protected, open waits" emerges from the penalty, with no
  reservation and no separate pass. Open orders contribute nothing to the breach.
- **Hard, at apply.** A no-regression backstop mirrors the existing worst-order
  backstop: an automatic re-optimization is applied only if it does not increase
  `max_committed_slip` (the single worst committed order's slip past its own promise)
  versus the plan it would replace — so re-optimizing can never make an
  already-promised delivery date worse.

**Physical slip is flagged, not hidden.** If a committed order is already heading past
promise+3 for a real reason (slow floor, breakdown — not re-optimization), the cap
cannot undo that: the convex penalty naturally *prioritizes* it (minimizing the
breach), and it is surfaced red ("past promise, +N d") on the Orders tab. The cap
governs the optimizer's *choices* among feasible plans, not physical reality.

- **All-open book:** `committed_promise_breach = 0` and `max_committed_slip = 0` for
  every plan — byte-identical to a book with nothing committed.
- **Measured cost (Test8, ~half the book committed):** the weight roughly halved the
  number of committed orders sitting past +3 (8→4 orders) and cut the worst slip from
  16 to 9 days, at a cost of ~2% more total late-days overall — the accepted price of
  the promise (decision #7 in the design spec).

### Scheduled optimize — the job order re-optimizes itself, twice a week *(feature, 2026-07-18, supersedes the event-triggered "self-tuning plan" of 2026-07-16 — superseded in turn by the daily-cadence "Frozen zone" rule below)*

The plan still re-optimizes itself without anyone remembering to click Optimize — but
the owner drew a hard line between two very different things:

- **Facts vs job order.** Every punch still replans **instantly** — quantities, dates,
  and the Gantt always reflect what actually happened on the floor, every day. But
  re-sequencing the batch *order* every time something changes was found to destroy
  schedule trust (the floor was seeing a different plan hour to hour). So the JOB ORDER
  — the thing a fresh Optimize contest can change — is only re-optimized **twice a
  week, automatically: Monday and Friday at 11:00 IST (05:30 UTC)**. Feedback is
  entered on the floor by ~10:00; the freshly re-optimized schedule is ready before
  shift 2 starts. With Thursday as the weekly off, Monday and Friday sit exactly 3
  working days apart in both directions — an even spread across the week.
- **No event triggers.** A masters upload, an order delete/clear, a commit/urgent/
  uncommit, an absence added/removed, and a Settings save no longer kick off a contest
  by themselves — they just take effect, and the next scheduled run picks them up. New
  orders arrive Open and sit there until either the next Monday/Friday run or the
  owner's own manual Optimize click — that manual flow (arrive Open → owner presses
  Optimize → commits) is untouched and still works exactly as before.
- **The Done button keeps its old meaning, not the contest.** The Capture Actuals
  "Done entering — update plan" button still means "I've finished punching today's
  numbers" — it refreshes the plan from those punches immediately and tells the clerk
  when the next scheduled optimization will run. It no longer starts a search of its
  own; punches were never supposed to trigger a re-sequence, and now nothing else does
  either except the twice-weekly schedule.
- **Cloud-only, one at a time, and skipped when nothing changed.** The scheduled run
  only fires a contest when the free GitHub Actions cloud compute is configured (see
  "Optimize plan" below); if it isn't, the run is skipped with a note rather than
  burning the free web instance for 20-40 minutes. It never overlaps a contest already
  in progress. And if the fingerprint of today's book (order keys, remaining qty,
  lane/promised date, absences) matches the one the currently-applied plan was computed
  from, it skips silently — there's nothing new to re-sequence.
- **Auto-apply only if strictly better.** Exactly as before: when the scheduled contest
  finishes, its best plan is compared against what users currently see (today's applied
  plan, or the plain plan if none is applied, replanned on today's book). It is applied
  automatically only if it is **strictly** better (never on a tie or a worse result) —
  including the overlap % if the settings sweep (below) found a better one. Either way
  a one-line note appears on the Orders tab, timestamped in local (IST) time: *"Plan
  auto-re-optimized 11:00 — 445 late-days (was 471), overlap 80 → 70"* or *"Checked
  11:00 — current plan still best."*
- **No off switch.** The twice-weekly schedule is always on in the deployed app; there
  is no button or setting to turn it off (`AUTO_OPTIMIZE=0` exists only as an internal
  test-isolation switch, never exposed).

### Frozen zone — in-progress work is pinned during daily re-optimization *(feature, 2026-07-29, supersedes the "twice a week" cadence above)*

The scheduled-optimize cadence above is superseded: **"Done entering — update plan"
now runs a re-optimization every day, not twice a week.** An *unrestricted* daily
re-optimization would be unusable — reshuffling machines/operators for work that is
physically running right now on the floor is impossible and would destroy schedule
trust. The fix is a **frozen zone**: whatever is genuinely in progress is pinned in
place; only the rest of the backlog is re-optimized.

- **What freezes.** A routing step whose punches show it **partially done** —
  `0 < good qty < required qty` for that (SO#, item, process). A step with `good == 0`
  (untouched) is free to optimize; a step with `remaining == 0` (fully punched) is
  already a zero-duration milestone and needs no pinning.
- **Pinned machine and operator** come from the **last-applied plan** — the schedule
  the floor was actually handed, not a re-derivation. **Remaining quantity** comes
  from the punches (`required − good`), so the frozen block reflects reality, not
  the plan's original estimate.
- **Resume order.** Multiple in-progress operations can share one machine; they
  resume in **previous-plan order**, and **all frozen work on a machine finishes
  before any newly-optimized work** is placed on it.
- **No setup on resume** — the machine is already set up mid-run; the 90-minute
  CNC/VMC setup (Rule 4) is not re-charged.
- **Shift crossing.** The machine stays pinned; if the remaining frozen work crosses
  a shift boundary, it hands off to a qualified operator on the new shift exactly as
  Rule 6's normal per-shift handoff does.
- **Absent planned operator.** If the operator the last-applied plan assigned is now
  marked absent, the engine staffs a **substitute** for the frozen block — the
  machine stays pinned, only the person changes.
- **Never frozen:** OS/outsourced steps and the DISPATCH milestone — they have no
  in-house machine/operator to pin.
- **Everything else re-optimizes freely** around the frozen blocks — this is what
  makes the daily cadence safe: only the physically-committed slice of the floor is
  locked, and the search still finds the best order for every operation that hasn't
  started.
- **Non-blocking edge cases:** no last-applied plan on file (first run / fresh
  import) → nothing to freeze, runs unrestricted; a step not found in the
  last-applied plan, or whose saved machine no longer exists in masters → not
  frozen, schedules normally (reported, never fatal).
- **The admin's manual "Start deep search" also respects the freeze** (owner
  decision, 2026-07-29 — deep-search is pressed in week 2 when week-1 work is
  already running): it recomputes and honours the same frozen set as the daily
  Done path. It is naturally unrestricted only when nothing is in progress yet
  (a fresh SO import, no last-applied plan).

### Optimize plan (sequence search) *(feature)*

Rule 6 is a greedy, single-pass scheduler: it builds exactly ONE plan, and the batch
sequence it consumes (Rule 3's order) is worth days of makespan by itself — measured on
the real book, better sequencing alone cut the plan from 42.5 to 39.7 days and total
late-days from 1,026 to 792. Because one full plan evaluates in under a second, the
**Optimize** button (admin) searches instead of trusting one pass: it tries many batch
sequences on the *current* order book, replays the **unchanged Rule 6** for each, scores
every plan by ONE symmetric on-time penalty — how far each order misses its delivery
date in either direction, ignoring the first 4 days, capped at 60 and squared so misses
spread across orders rather than concentrating — plus a 0.1 makespan tie-break, and the
dormant worst-order-ceiling and committed-promise guards (**delivery gaps dominant**, the
owner-chosen priority: favour fewest/smallest late deliveries over the very shortest
finish, because on the real book the shortest-finish plans push *more* orders late), and
keeps the best.

- **Multi-start search.** A single hill-climb from one seed gets trapped in whatever local
  optimum it first descends into (measured: it stalled at 39.75 d / 778 late-days on
  Test5, and adding plans didn't help). So the search runs **many independent restarts** —
  the strong dispatch heuristics (SPT, ATC) then an unlimited stream of fresh random
  permutations — hill-climbs each with insertion/swap/block moves until it stalls
  (`_RESTART_AFTER`), and keeps the **global best** across all restarts. This reliably
  reaches better basins (Test5: → 39.7 d / **713 late-days** / 44 late). Fully
  deterministic (every restart's RNG is seeded off the base seed); never skips a run.
- The UI exposes one **Deep search** (owner decision). On the production (new) engine it
  runs **~1,800 plans in the cloud** (12 fine-grid overlap candidates × 150) or **~200
  locally**. Budgets are **evaluation counts** with a fixed random seed, so the same
  book + settings + budget always yields the same result on any machine. The scheduler is memoized (invariant machine-name parsing and
  per-day work-windows are cached), so each plan evaluates ~3.5× faster with identical
  results.
- **Settings sweep (2026-07-15, fair contest).** The same click also auto-tunes the
  **overlap %** (`optimizer.sweep_optimize`): the current overlap plus candidates
  50/60/70/80 (90/100 dropped — they lost every measured contest on both real books)
  each get the **SAME search depth** — the total budget split equally across the
  contenders — and the best-scoring plan wins outright. The current setting runs first
  (an early Stop still leaves it fully searched) and wins exact ties (no Settings
  churn); that is its only privilege. Apply persists the winning overlap **into the
  saved plan config** (openly visible in Settings) alongside the ranks, and the
  staleness fingerprint is computed against the winning settings. Result panel reports
  "Best setting found: Overlap X%". Spec:
  `docs/superpowers/specs/2026-07-15-optimize-settings-sweep-design.md`. Every lane
  competes in the same one pool (see "Order commitment (lanes)" above) — no lane gets
  a promise guard or reserved capacity in the search; only operator absences (below)
  reserve time.
- **Machine-set sweep (2026-07-29, third dimension, new engine only).** Optimize now
  searches **sequence × overlap % × machine-set**: for each plan it also tries
  **Allotted-only** vs **Allotted + Suggested (deduped union)** as the option set every
  in-house machining/manual/inspection op can be placed on, keeping the single best
  `(machine-set, overlap, sequence)` combination by the same score. `flexible_machines`
  (config field, default `False` = today's Allotted-only behavior, byte-identical) is
  **optimizer-owned** — never hand-edited, set only by applying a winning contest result,
  exactly like the tuned overlap %. The winner persists into the saved plan config and
  every subsequent Plan reproduces it (`_new_masters` loads masters at that flexibility).
  Because it doubles the work (both machine-sets swept in full), the contest is roughly
  **2× the cost** of the sequence×overlap search above (cloud ~15→~30 min; local
  ~200→~400 plans) — the progress bar sizes itself accordingly on the new engine only.
  Classic/flow ignore this flag entirely (single-pass, their own machine selection via
  `rule6_allocate._resolve_candidates`/`split_parallel`). Spec:
  `docs/superpowers/specs/2026-07-29-machine-set-optimize-dimension-design.md`.
- **Cloud compute (2026-07-15).** When configured, clicking Optimize runs the full fair
  contest on a free GitHub Actions runner instead of the reduced local fallback — same
  code, byte-identical results either way. On the production (new) engine that is
  **~1,800 plans** (12 overlap candidates × 150, ~15 min) in the cloud vs **~200** local
  **per machine-set** (now doubled by the machine-set sweep above).
  (The retired classic engine used 6 candidates × 400 = 2,400 cloud / 1,000 local.) Falls
  back to local automatically if the cloud is unavailable or times out.
- The admin sees a before/after table and chooses **Apply** or Discard. Apply persists
  a **rank per (SO No, Item Code)**; every subsequent Plan replays it
  (`pipeline.apply_priority_rank`): ranked batches reorder among the slots they already
  occupy, **unranked (new) orders keep their natural Rule-3 slot** — a fresh urgent
  order is never pushed to the back. A banner flags how many orders were added since
  the last optimization ("re-optimize for the best plan"); **Remove optimization**
  reverts to the pure Rule-3 order. A contest may also be started automatically twice a
  week by the scheduled optimize (above) and auto-applied.
- **Replay guarantee:** feeding the saved ranks back through the pipeline reproduces
  exactly the metrics the search reported (tested) — **for the same inputs**. The
  applied result carries a **fingerprint of the masters workbook + the plan-shaping
  settings** it was computed on (`inputs_sig`); when the owner later re-uploads an
  edited workbook or changes Settings, the replayed numbers legitimately differ, and
  the banner says so ("Settings or masters have changed since this optimization —
  run Optimize again") instead of looking non-deterministic (live 2026-07-15 finding:
  "the same Deep run gave two results on two days" — the masters had changed between
  them). Schedule-neutral knobs (balance workload; expedite, which is forced off under
  ranks) are excluded from the fingerprint.
- **Default: off.** With no applied optimization every plan is byte-identical to today
  (golden trace unchanged).

### Operator absences *(feature, 2026-07-16)*

The admin can mark a named operator absent for a date range (day granularity), from a
dropdown of the operators in the current masters — a genuinely physical constraint,
unlike the informational commitment lanes above:

- **The person is unavailable in every plan and every Optimize contest** for the whole
  window (inclusive) — their scheduled work is pushed to a qualified, free colleague or
  to after the absence, the same mechanism Rule 6 uses for any reserved time.
- **Analytics stays honest:** an absent operator's available capacity for those days is
  subtracted before computing their utilization %, so they don't show as "idle" while
  actually on leave.
- **Orphaned absences (masters re-upload removed the operator)** are ignored by
  planning and reported as a non-blocking row in the validation banner
  (`ABSENT_OPERATOR_UNKNOWN`) — never fatal, same forgiving pattern as a pending
  machine/routing.
- **Visibility:** the absence list is visible to both roles (Settings area); only the
  admin can add or remove an entry. No password re-confirmation (reversible, not
  destructive).
- **Default:** no absences on file ⇒ every plan is byte-identical to today.

---

## PHASE 5 — Execute and re-plan *(closed loop)*

### 🔻 Rule 7 — Capture daily actuals  *(Pipeline stage)*
**After every shift / next morning**, enter the previous period's production via
the **Daily Production Entry** form. Fields captured:

- *Identity (manual):* Date, Shift, SO No, Item Code. *Auto/dropdown:* Item Name
  (auto-prompted from the routing) and Process (dropdown of the item's routing).
  **Operator** (required dropdown, 2026-07-18 — from the operator master; the
  entry is rejected without one) — who ran this process, feeding the monthly
  **Operator efficiency report** below.
- *Output (manual):* Qty Produced, Qty Rejected, **at the named Process**. **Good qty
  = produced − rejected** for that step. Good at the **finished-goods gate** (the
  DISPATCH step, or the last step if the routing has no DISPATCH) is what **fulfils
  the order** and reduces its remaining qty. Good at an **earlier** step is
  **work-in-progress**: it is recorded (and lets the next Plan skip that finished
  work — see Rule 6 "continue from reality"), but it does **not** reduce the order's
  remaining qty — those pieces still owe every later step. (This fixed a bug where
  any step's production was wrongly counted as finished goods.)
- *Actual setting time (manual):* the real setup vs the planned `setup_time_min`.
- *Downtime/loss categories (manual, minutes):* No Power, No Operator, Tool
  Problem, Machine Breakdown, No Load, Other Work — summed per item code into a
  total-downtime rollup for analysis. Plus a free-text Remarks.

- **Source:** original Rule 7 + `Sample entry window format`
- **Input:** the schedule (Rule 6) + manual entry via the Daily Production Entry form
- **Output:** actuals data (durable store) + a per-item-code output/downtime
  rollup shown on the Rule 7 tab
- **One day at a time:** the "Saved entries" list shows only the **latest punched
  date's** entries, and **only those can be rolled back** (server-enforced); once a
  newer date is punched the previous day locks and drops off the list. This keeps the
  list from growing without bound across 20–50 SOs × processes × days. Every entry
  stays permanently in the record and in the per-item rollup (which sums **all**
  entries) — only the editable *list* is scoped to the current day.

### 🔁 Rule 8 — Re-run MRP and regenerate the plan  *(Loop)*
After actuals are entered, **re-run MRP/refresh**: regenerate the plan from
**actual completed qty + balance remaining**, looping back to Phase 1.

- **Source:** original Rule 8
- **Input:** actuals (Rule 7) + balance remaining
- **Output:** triggers a fresh run starting at Rule 1
- **Implementation note:** realized as the unified **"Plan"** over the persistent
  order book — it emits every active order at its remaining qty (ordered − **finished**
  good at the gate) **plus its per-process remaining** (`process_qty`), and re-runs
  Rules 1–6. So the re-plan is **dynamic**: it reflects exactly the quantity the floor
  punched in per process, continues from there, and refreshes the
  schedule, machine allotment, Gantt and Orders tab everywhere. "Run" and "Rerun MRP"
  are one action; there is no dedicated rule module (`orderbook.active_so_lines` does this).
- **Plan clock advances past completed days** (`orderbook.effective_plan_start_date`):
  once a day's production is punched, the re-plan starts from the **next working day's
  first shift** after the latest actual's date — that day is done and over. So punching
  1 March's output makes the remaining work start **2 March 08:00**, not restart from
  1 March. It never moves earlier than the configured `plan_start_date`, and skips
  weekly-off/holiday days. With no actuals recorded, the plan starts from the configured
  date exactly as before (golden trace unchanged).
- **Plan clock starts from the current date automatically** *(live mode, 2026-07-18)*:
  `config.plan_start_date` is **nullable** — `None` means "auto: start from **today**
  (Indian time)". The **live default is `None`**, so every plan the shop runs begins
  from the real current date without anyone editing a setting. A **fixed date** is a
  testing/reproducibility override (the golden trace pins `date(2025, 3, 1)`). The pure
  engine never sees `None`: the API boundary resolves it to today (IST) via
  `api.main._resolve_config` / `_ist_today()` before any rule runs. The **saved** config
  keeps `None` (never a resolved date), so a moving "today" is never mistaken for a
  settings change. Settings UI: the **"Start from today (recommended)"** checkbox
  (`cfg-start-auto`) — checked sends `plan_start_date: null` and disables the picker.

### Operator efficiency report *(feature, 2026-07-18)*

Every Capture Actuals entry now **names the operator** who ran it — a required
dropdown, chosen from the app's operator master, same list Settings shows.
A blank or unrecognised name is rejected (the entry isn't saved). This lets the
admin pull a **monthly efficiency report per operator**, downloadable as a CSV,
built purely from a fair formula — never biased by which shift, machine, or
job mix an operator happened to draw:

> **Efficiency % = Earned minutes ÷ Attended minutes × 100**
> - **Earned** = the *standard* cycle time for each item/process (from the
>   routing) × the **good** pieces punched — reject pieces earn nothing.
> - **Attended** = the shift window the operator actually worked, minus **all**
>   recorded downtime and setup time for that window (downtime/setup are
>   *neutral* — they shrink the time judged, they never count for or against
>   the operator).

Fairness guarantees baked into the formula:

| Situation | How it's handled |
|---|---|
| Rejected pieces | Earn nothing; surfaced separately as **Reject %** — never blended into efficiency |
| Downtime (power/tooling/breakdown/etc.) and setup time | **Neutral** — subtracted from the attended window, so a stoppage that isn't the operator's fault neither helps nor hurts their score |
| A punched item/process with **no cycle-time standard** on file | **Excluded from the formula on both sides** — it earns nothing AND its shift time isn't charged to attended either, so nobody is graded against a standard that doesn't exist. Counted separately in a **"No-standard punches"** column so it's visible, not silent |
| Approved leave | A separate **Days absent** column, from the absence list — never mixed into the pace calculation |
| Old entries from before this feature | Grouped into one **"Unattributed"** row (no operator name was captured back then) |

**Shift text is normalized** (case-insensitive: "1st shift"/"first"/"1" → first,
"2nd shift"/"second"/"2" → second — the Capture form's Shift field is a
dropdown limited to "1st shift"/"2nd shift" so it can't be free-typed);
unrecognized/blank shift text falls to the manual (day) window, a documented
fallback rather than a silent miscount.

**Report columns** (one row per operator, for a chosen calendar month): Operator ·
Days worked · Days absent · Attended (min) · Earned (min) · Efficiency % · Pace
vs standard (attended ÷ earned) · Good qty · Rejected qty · Reject % · Downtime
(min) · Setup (min) · Jobs handled · No-standard punches.

**Where to get it:** Settings → **Operator efficiency** (admin only) — pick a
month (defaults to last month), **Preview** to see it on screen, or **Download
CSV** for the file (`operator-efficiency-YYYY-MM.csv`). Purely a report: running
it, previewing it, or downloading it never touches the schedule/plan.

**Nothing is ever deleted.** Every punch stays in the database permanently (a
full year of daily entries is only a few MB) — the report is simply computed
on demand for whichever month is picked, so any past month can be pulled up
later.

---

## Configurable parameters (summary)

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process (CNC/VMC only) | 90 min | Rule 4 |
| Operation overlap | always on, 50% (configurable) | Rule 5 |
| Apply operator & shift logic | on (UI) | Rule 6 ← masters |
| Two-shift threshold | 12 hrs (Available Hrs/Day) | Rule 6 |
| Manual / single-shift window | 09:00–18:00 | Rule 6 |
| Split alternative machines in parallel | on (UI) | Rule 6 |
| Expedite window (least-slack tie-break) | 0 min = off (engine) | Rule 6 |
| Balance operator workload (schedule-neutral) | off (engine) | Rule 6 |
| Committed-promise slack (`committed_promise_slack_days`) | 3 days | Order commitment |
