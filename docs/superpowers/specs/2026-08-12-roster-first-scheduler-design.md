# Roster-first scheduler + two-dimensional optimizer — design

**Date:** 2026-08-12
**Status:** approved for planning
**Repo:** `riittiin/anvitech-ppc-duplicate`, branch `main` (the duplicate — the live
`anvitech-ppc-engine` repo is never touched, per the standing rule)

---

## 1. The problem

The owner's report: *"there is a lot of scope — free machines plus free operators who
can operate that machine, and available work — but due to some reasons it doesn't do
that."*

A previous attempt to fix this by patching the existing scheduler ended up violating
the shop's own rules. This design rebuilds the scheduler and the optimizer from
scratch instead, around the rule set below, so the rules hold **by construction**
rather than by check.

---

## 2. Cross-check: what today's engine actually does

Read from the code before any of it changed. Three of the four rules are not being
followed.

### 2.1 Rule 1 (one operator ⇄ one machine ⇄ one shift) — not enforced

`ppc_engine/scheduler/staffing.py` opens by claiming the rule is true "by
construction", then implements the opposite. The 2026-07-24 *short-job exception*
books an operator only for the minutes the job runs, and `_assign` is documented as
*"a soft preference for machine stability, **not a hard lock**"*.
`flow_scheduler._lay_on_machine:552` states it outright: *"the short-job exception,
which lets an operator freed by a short job elsewhere cover this machine."*
Operators hop machines mid-shift today.

### 2.2 Rule 2 (no segmentation) — not enforced

Four mechanisms fragment work:

| Mechanism | Effect |
| --- | --- |
| `StaffingBoard.longest_available_prefix` (commit `3244560`) | Takes "the longest stretch somebody IS free for" — slices an operation down to `min_slice_min = 30` minutes. |
| `machine_busy` calendar (commit `ab9339b`) | Exists so a **later** op can be dropped into an **earlier** hole → a machine's timeline interleaves jobs. |
| `flow_scheduler._try_split` | Splits one operation across up to 3 machines in parallel. |
| `ppc_engine/scheduler/gap_harvest.py` | Moves *part* of a job into a hole and trims the tail off its main block. |

### 2.3 Rule 3 (overlap) — the percentage is inverted, and there is no piece rounding

`RULES.md:114` and `ppc_engine/config.py:109` both define overlap as *"0.9 = start at
90% done"*. `flow_scheduler._ready_after:682` computes:

```python
release = start + timedelta(minutes=setup + (1.0 - config.overlap) * cutting)
```

At `overlap = 0.9` that releases the successor when **10%** of the pieces exist. The
two definitions coincide only at exactly 50%, which is why this survived. The
optimizer has been converging on 88–95 — i.e. *"start the next process when 5–12
pieces of 100 exist"* — after which the piece-flow guard re-lays the operation later
to repair the impossibility it just created. The release is also a continuous time
fraction, so it never lands on a whole-piece boundary.

### 2.4 Rule 4 (90-min setup on process change) — charged, but unconditionally

`ppc_engine/scheduler/duration.py` bills 90 minutes for *every* machining operation,
including one that follows the same `(item, process)` on the same machine. This is
conservative, not a violation.

---

## 3. The rules the new engine must satisfy

1. **One operator, one machine, one shift.** An operator mans exactly one machine for
   a whole shift. He may change machine only at the next shift, and only to a machine
   on his Settings machine list. **Binds CNC/VMC only** — helpers and inspectors
   physically walk between manual/inspection stations, and forbidding that would
   delete capacity that really exists.
2. **No segmentation.** An operation runs to completion on its machine, uninterrupted.
   It may span shift ends, off-days and unmanned shifts — the part stays in the chuck
   and the machine is held — but no other job may be squeezed in, and the operation is
   never sliced.
3. **Overlap in whole pieces.** With overlap `p` and batch `qty`, the successor is
   released once `ceil(p × qty)` pieces have cleared. `p = 0.8` → 80 of 100 pieces.
   OS and dispatch never overlap.
4. **Setup on change.** 90 minutes on a CNC/VMC whenever the machine's previous job was
   a different `(item, process)`. Same part, same side, back to back → the fixture is
   already on → no setup. Manual/inspection: no setup, as today.

Jobs may move between machines and a machine may run different jobs in sequence.
Operators may not move within a shift, and an operation is never broken up.

---

## 4. Architecture

**`roster_engine/`** — a new top-level package with **zero imports from
`ppc_engine/`**. It consumes the app's already-loaded objects (`engine.models`:
`Masters`, `SOLine`, `Config`, plus the frozen set from `engine/freeze.py`) and emits
the app's `list[ScheduleEntry]`.

Reused unchanged: the Excel loader, order book, durable store, freeze computation,
API, Gantt, Schedule tab, delay report, analytics, shift-wise export, Daily Entry,
efficiency report, login/roles.

Wired at the existing `engine/pipeline.py::scheduler_for(config)` seam as
`"roster"`, selected by `DEFAULT_SCHEDULER=roster`. Nothing changes until that env var
is set; switching back is one env var.

```
roster_engine/
  domain.py      internal view of shop + jobs, built from engine.models
  assign.py      max-weight bipartite matching (Hungarian, pure Python)
  roster.py      who mans what, per shift
  scheduler.py   flow jobs into the capacity the roster creates
  release.py     overlap in whole pieces + pacing + dispatch
  objective.py   score a plan (same formula as today)
  search.py      alternating sequence x crew search
  report.py      the four violation checks
engine/roster_adapter.py   Masters/SOLine/Config in, ScheduleEntry out
```

No new third-party dependency: `requirements.txt` has no scipy, so the assignment
solver is ~80 lines of pure Python in `assign.py`.

---

## 5. Part 1 — the scheduler

The Giffler-Thompson event loop is replaced by a **shift clock**:

```
for each shift S in the horizon:          # Mon-1st, Mon-2nd, Tue-1st, ...
    1. ROSTER  — decide who mans what for S
    2. RUN     — advance every manned machine through S
    3. advance
```

### 5.1 ROSTER — `roster.py` + `assign.py`

**Rosterable machines** are CNC/VMC only, by the same predicate the setup rule uses
(id `CNC*`/`VMC*`, or Machine-master type CNC lathe / Vertical Machining centre).
Manual, inspection, OS and dispatch are not rostered: a manual/inspection op is
staffed by any qualified operator who is on that shift, present, and not already
occupied at that moment — no per-shift lock, because a helper walks between stations.

**Eligibility** of operator `o` for machine `m` in shift `S` on date `D`:

- `m ∈ o.qualified_machines` — exactly the Settings machine list, nothing else
- `effective_shift(o, D) == S`
- `o` is available (shop open, not on leave/absence)

**Value** of the pairing:

```
v(o, m, S) = min(shift_minutes, runnable_minutes(m, S))
           + CARRY_BONUS      if a job is physically mid-run on m
           + LOOKAHEAD_UNIT * (n_machines - crew_rank[m])
```

- `runnable_minutes(m, S)` — minutes of work that can actually start or continue on
  `m` during `S`, given what the routing has released by then.
- `CARRY_BONUS` is large enough that continuing an in-progress part always wins. This
  is what makes "no segmentation" survive the shift boundary.
- The last term is the **crew genome** (§6.2): the optimizer's strategic lever.

`CARRY_BONUS` and `LOOKAHEAD_UNIT` are calibrated during implementation against the
owner's real book, not chosen by taste: `CARRY_BONUS` must exceed the largest possible
`runnable_minutes` (a full shift), and `LOOKAHEAD_UNIT` is swept so the crew genome is
strong enough to move a decision but never strong enough to man a machine with no work.

**Solve** the max-weight bipartite matching exactly. Each operator gets at most one
machine; each machine at most one operator. Deterministic tie-break by
`(crew_rank[m], o.name)`.

The owner's original complaint becomes the *objective of this step*: a machine with
ready work cannot be dark while a qualified operator sits unassigned, because the
matching would have scored that pairing positively. It is asserted afterwards and
reported as `idle_capacity_violations` (§7).

### 5.2 RUN — `scheduler.py`

Machines are advanced through shift `S` in `crew_rank` order (deterministic, and it
gives a higher-ranked machine first pick of a job both could run).

Per machine:

- **Job in progress** → continue it. **No fresh setup.**
- **Otherwise** → take the next ready job from its queue, ordered by the **job
  sequence** genome, ties by delivery date, then key. A job is ready when its routing
  predecessor has released enough pieces (§5.3) and it has not started.
- **Setup:** 90 minutes if `(item, process) != machine.last_setup_key`, else 0.
- **Lay** `setup + qty × cycle` **contiguously** in worked minutes, spilling across
  shift ends, off-days and unmanned shifts. The machine is **held** from job start to
  job end. Segmentation is not prevented by a check — it is inexpressible.

**Output:** one `ScheduleEntry` per operation, with `op_segments` carrying one segment
per shift crossed, naming that shift's rostered operator. A job started at 17:00 that
runs into the night shows `Narayan → Sidhu`, which is the owner's 5 p.m. example.

**Frozen (in-progress) work.** The freeze set pins machine and operator. A frozen op
forces its machine to be rostered (via `CARRY_BONUS`) and prefers the pinned operator
when he is still qualified for that machine and on that shift; otherwise a substitute
is rostered and the machine pin stays. Remaining quantity is taken from
`Order.process_remaining` at **batch** level — never per SO line (the 2026-08-11
lesson). No setup on resume.

### 5.3 RELEASE — `release.py`

```
released_pieces = ceil(overlap × qty)
successor ready once the machine has WORKED  setup + released_pieces × cycle_min
```

The offset is measured in **worked minutes**, not wall-clock, so an overnight gap
cannot release pieces that were never cut.

- `overlap = 0.8` → 80 of 100 pieces. Owner's definition, `RULES.md`'s definition, and
  `ppc_engine/config.py`'s own docstring. The current code computes the complement.
- OS and dispatch stay fully sequential, both directions.
- A no-cutting step (no cycle time) does not overlap — its successor waits for full
  completion (`RULES.md:127`).
- **Pacing** retained: a successor's end is never before its predecessor's end. With
  the corrected direction this rarely binds, instead of binding constantly.
- **DISPATCH** is placed at the latest end across all of the batch's operations.

---

## 6. Part 2 — the optimizer

Two dimensions are searched, not one: **the crew** and **the job flow**.

### 6.1 What is solved, not searched

Within one shift, "who mans what" given a value per (operator, machine) pair is a
maximum-weight bipartite matching — an exact algorithm, 20×26, well under a
millisecond, ~100 shifts per plan. It is noise next to the job simulation.

So the optimizer never spends an evaluation on a roster that is locally dominated;
every plan it looks at already carries the best crew arrangement *for the priorities
it was given*. Consequence for cost: the crew dimension spends **evaluations**, not
per-evaluation wall-clock — unlike `flexible_machines`, it does not double Actions
time.

### 6.2 What is searched: the lookahead the matching cannot have

The matching's value is *"minutes ready right now"*. That is short-sighted: it will
man the machine busy at 08:00 and leave dark the machine whose big batch releases at
11:00, or on Thursday.

**Crew genome = a permutation of the rosterable machines** — their claim priority on
scarce operators, entering the value as `LOOKAHEAD_UNIT * (n - rank)`. Deliberately a
*permutation*, because it is the same object type as the job sequence, so the
insertion / swap / block moves already proven in
`ppc_engine/optimize/search.py::_local_search` transfer unchanged.

### 6.3 The two dimensions, searched together

A joint random walk over two genomes wastes evaluations. Alternating descent does not:

```
restart from a seed (SPT / EDD / slack / random)
repeat until neither phase improves:
    Phase J — freeze the crew genome, hill-climb the JOB SEQUENCE
    Phase C — freeze the sequence,    hill-climb the CREW PERMUTATION
keep the global best across restarts
```

Both phases accept only strict improvements; deterministic under a fixed seed, so
"what you Apply is what you get" holds. The J/C split starts at ~60/40 and is
**measured**, not assumed.

### 6.4 Overlap — the outer dimension, continuous 50–100%

One overlap per plan (owner's decision), chosen by the optimizer, never typed by a
user. Searched **continuously across 50–100%**, not on a coarse grid.
`optimize_service.cloud_candidates` already fans one GitHub Actions shard out per
overlap candidate; each shard now runs the alternating search of §6.3 instead of a
sequence-only one.

Under the corrected definition, 50–100% is exactly the physically sane band. Today's
engine tuned to "88–95" is really running 5–12%.

### 6.5 Objective — deliberately identical

`roster_engine/objective.py` is written fresh but reproduces the **same formula**:
symmetric on-time penalty (`|miss| − band`, capped, squared) plus a 0.1 makespan
tie-break, plus the dormant ceiling / committed-promise guards. Identical scoring is
what makes the A/B against the live engine honest.

### 6.6 What changes, file by file

| File | Change |
| --- | --- |
| `roster_engine/*` | **NEW** — everything above. |
| `engine/roster_adapter.py` | **NEW** — `Masters`/`SOLine`/`Config` in, `ScheduleEntry` out. |
| `engine/pipeline.py` | `scheduler_for()` gains `"roster"`. |
| `engine/optimizer.py` | `optimize`/`sweep_optimize` delegate to `roster_engine` when `scheduler == "roster"` — same shape as today's `new_engine` delegation. |
| `engine/optimize_service.py` | `build_payload`/`parse_payload` round-trip the crew genome beside the sequence ranks; `ContestSetup` and `run_candidate` carry it. |
| `api/main.py` | `_optimize_apply` persists the winning crew genome next to the ranks and overlap, so every later plan **replays the same roster** — a stable crew for the floor, not a fresh one per refresh. |
| `.github/workflows/optimize.yml`, `scripts/cloud_optimize_worker.py` | **UNCHANGED** — they carry a slightly larger JSON. The Actions button works on day one. |
| `ppc_engine/*`, `engine/rule*`, `web/*` | **UNTOUCHED.** |

---

## 7. The proof — violation checks on every plan

Four pure functions in `roster_engine/report.py`, siblings of the existing
`routing_order_violations` / `qualification_violations`, appended by
`api._report_for_book` (non-blocking) and run against **both** engines so the
side-by-side comparison is evidence rather than assertion:

| Check | Flags |
| --- | --- |
| `operator_split_violations` | Anyone planned on two machines within one shift. |
| `segmentation_violations` | Any operation whose machine time is interrupted by another job. |
| `idle_capacity_violations` | A machine with ready work, dark, while a qualified operator sits unassigned. |
| `overlap_rounding_violations` | Any successor released on a fractional piece. |

The existing routing / qualification / batch-quantity checks continue to run.

---

## 8. Rollout and the testing loop

The owner runs the tests; the assistant cannot. The loop, agreed:

1. Write the code, push to `main` of `riittiin/anvitech-ppc-duplicate`.
2. Owner runs the optimizer from GitHub Actions.
3. Assistant posts the results — both engines, same book, with violation counts.
4. Owner takes the call.

`DEFAULT_SCHEDULER` stays `new` until the owner switches it, so a push cannot change
what the duplicate site serves.

**Acceptance, decided up front:** *legality first*. A plan the floor can execute beats
a shorter plan that assumes one man runs two machines at once. If the roster engine
reports more late-days than today's engine while reporting zero violations, that is
the honest baseline and we optimise from there — the same call already made for the
piece-flow guard (Test8 1214 → 1323 late-days: *"the old numbers were infeasible, not
better"*).

---

## 9. Risks accepted

- **Throughput will look worse before it looks better.** Today's engine gets capacity
  from operators covering two machines in a shift. That capacity is not real. Removing
  it lengthens the plan.
- **Dark machines are now visible.** With ~20 operators and 26 machines, at most 20
  machines can be lit in a shift and fewer at night. This is a true constraint that the
  current plan hides.
- **A held machine can block.** A job spanning an unmanned shift holds its machine
  idle. That is physically correct (the part is in the chuck) but will appear as idle
  time in Analytics.
- **The crew genome is a heuristic lever, not an optimum.** It encodes lookahead the
  matching cannot have; whether the search actually finds value in it is a measured
  question, answered in step 3 of the loop above.

---

## 10. Non-goals

- No UI change, no new screens, no new reports beyond the four checks in §7.
- No change to the loader, order book, store, freeze, API contracts or the plan cache.
- `ppc_engine/` and the classic/flow engines are untouched and stay green.
- No per-item overlap (§6.4), no roster searched per shift explicitly (§6.1).
- The live `anvitech-ppc-engine` repo is not touched.
