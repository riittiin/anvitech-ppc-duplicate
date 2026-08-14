# CP scheduler + optimizer (PyJobShop) — design

**Date:** 2026-08-14
**Status:** approved for planning
**Repo:** `riittiin/anvitech-ppc-duplicate`, branch `main` (the duplicate — the live
`anvitech-ppc-engine` repo is never touched, per the standing rule)
**Supersedes, for the live path:** `2026-08-12-roster-first-scheduler-design.md`
(§5 search, §4 roster). The roster engine is retired, not deleted.

---

## 1. The problem

Two engines currently split one job. `roster_engine/scheduler.py` decides where and
when each operation runs, using a greedy shift clock; `roster_engine/search.py`
searches the job sequence and a crew permutation around it by alternating descent.
Neither can see the decision the other is making, so the search re-runs a greedy
dispatcher thousands of times and hopes a better opening order rescues it.

`scripts/tardiness_bound.py` already measured what that costs. It built a
constraint-programming model of the same book and found schedules the engine could
not reach. Handing the solver's **job order** back to the engine recovered little,
and the script says why in its own closing text:

> The solver's advantage lives in decisions the engine does not take from a
> sequence — which machine, which operator, which shift — so handing over the job
> ORDER alone hands over none of it.

That is the finding this design acts on. The solver keeps its advantage only if it
keeps its *decisions*, so the solver becomes the scheduler and the optimizer, and
the app stores the whole decision set rather than a job order.

Two further defects in today's model are fixed on the way:

* it **relaxes away two of the shop's four rules** — Rule 1 entirely, Rule 4 for
  tractability (`tardiness_bound.py:384-396`) — so its answer is a floor, not a plan;
* it **inherits the engine's answers as constraints** — Allotted-only machines, one
  globally tuned overlap % baked into every precedence — so it is not free to
  optimize the things a job-shop solver is good at.

---

## 2. The four rules — mandatory, unchanged

Restated verbatim from `2026-08-12-roster-first-scheduler-design.md` §3. These are
constraints, not objectives, and nothing in this design weakens them.

1. **One operator, one machine, one shift.** An operator mans exactly one machine
   for a whole shift. He may change machine only at the next shift, and only to a
   machine on his Settings machine list. **Binds CNC/VMC only.**
2. **No segmentation.** An operation runs to completion on its machine,
   uninterrupted. It may span shift ends, off-days and unmanned shifts — the part
   stays in the chuck and the machine is held — but no other job may be squeezed in,
   and the operation is never sliced.
3. **Overlap in whole pieces.** With overlap `p` and batch `qty`, the successor is
   released once `ceil(p × qty)` pieces have cleared. OS and dispatch never overlap.
4. **Setup on change.** 90 minutes on a CNC/VMC whenever the machine's previous job
   was a different `(item, process)`. Same part, same side, back to back → no setup.
   Manual/inspection: no setup.

---

## 3. What is freed

Every restriction below is in today's model and **none of them is one of the four
rules**. Each is removed so the solver decides it.

| Restriction today | Where | After |
| --- | --- | --- |
| Modes are `(machine × qualified operator)` pairs; one person owns a whole operation | `tardiness_bound.py:348` | Modes are `(machine)` for machining ops. The operator is **not** a per-task choice |
| Machine options = Allotted, Suggested only as fallback | `roster_engine/domain.py::_candidates` | Allotted ∪ Suggested, deduped, Allotted first |
| One global tuned overlap %, baked in as a fixed start-to-start lag | `tardiness_bound.py::_release_delay` | ~~`p_j` decided per job, still `ceil(p × qty)` whole pieces~~ **Superseded (§5.3): `k_j` is provably always 1 — maximum overlap, always. The knob is gone because there is nothing to sweep, not because the solver tunes it.** |
| Job order supplied by Rules 2–3 and searched by local descent | `roster_engine/search.py` | Solver sequences from scratch; no seeded order |
| Rule 1 dropped, Rule 4 dropped | `tardiness_bound.py:22, 384` | Both enforced (§5.1, §5.4) |

Dropping the operator out of the mode is the load-bearing one, and it is **more
correct as well as cheaper**. A machining operation legitimately spans a shift
boundary (Rule 2), and the person running it after the boundary is whoever is
rostered next. Binding one operator to a whole task — what today's model does —
is a restriction the shop does not have. Removing it also deletes the
`machine × operator` product from every machining task's mode list, which is the
budget Rule 1 and Rule 4 now spend.

**Consequence, stated explicitly:** operators enter the model in *two different
ways*, and the split follows Rule 1's own scope.

* **Machining (CNC/VMC)** — the operator comes from the **roster** (§5.1), never
  from the mode. `ScheduleEntry.operator` and `op_segments` are derived by cutting
  the operation at shift boundaries and naming the rostered person for each piece.
* **Manual / inspection** — Rule 1 does not bind these; a helper physically walks
  between deburring and packing. The operator stays a **mode resource** (a
  capacity-1 renewable, free choice per task), exactly as today.

---

## 4. Architecture

```
cp_engine/                 the model and the solve — no app types inside
  domain.py       jobs/ops/shop, built from engine.models (adapted from roster_engine.domain)
  windows.py      per-task time windows; shift calendar in solver minutes
  model.py        builds the PyJobShop Model (native layer)
  rules.py        the four rules on the CpModel (escape-hatch layer)
  objective.py    lexicographic total-then-spread tardiness
  solve.py        build -> solve -> Solution
  genome.py       Solution <-> the stored decision genome
  decode.py       genome + today's book -> laid-out times (the fast replay)
  report.py       drift + rule checks

engine/cp_adapter.py       the seam: Masters/Batch/Config in, list[ScheduleEntry] out
```

`cp_adapter.py` is the only file that knows both worlds, mirroring
`engine/roster_adapter.py` exactly — same `run(batches, config, ...) ->
list[ScheduleEntry]` contract, same `op_segments` shape, same two off-lane literal
strings, same frozen-row translation. Everything downstream is untouched.

### 4.1 The two layers, and why there are two

PyJobShop's `Model` is used for everything it expresses well — tasks, modes,
machines, renewables, breaks, precedences, optional intervals. Then:

```python
data = model.data()
cp   = CPModel(data)          # pyjobshop.solvers.ortools.CPModel
rules.add(cp.model, cp.variables, ...)      # Rule 1, Rule 3, Rule 4
objective.add(cp.model, cp.variables, ...)  # replaces pyjobshop's objective
result = cp.solve(time_limit=..., num_workers=...)
```

`CPModel(data, model=None)` accepts an external `CpModel` and exposes `.model` and
`.variables`. `Variables` exposes `job_vars` (an interval per job, `.end` = the
order's completion), `task_vars`, `mode_vars`, `tardiness_vars`, and
`assign_vars[(task_idx, resource_idx)]` — an **optional interval with a `present`
boolean per task-resource pair**, which is precisely the handle Rule 1 needs.

**Verified, not assumed** (pyjobshop 0.0.9, scratch venv, 2026-08-14): building via
`CPModel(data)`, adding an own boolean, adding an own `model.minimize()`, solving,
and decoding all work; the custom objective **overrides** pyjobshop's. A three-task
smoke model returned `SolveStatus.OPTIMAL` with objective `250001` = 250000·1
late-day + 1·1², on a correct schedule. This is reproduced as
`tests/test_cp_escape_hatch.py`, which is the canary for a pyjobshop upgrade.

### 4.2 Two things pyjobshop cannot do, established by reading its source

1. **Breaks are static.** `Variables.BreakVar` pre-computes every possible break
   duration per mode as discrete choices keyed on start-time domains. A break can
   therefore never depend on a decision — which is exactly what "this shift is
   unstaffed" is. Rule 1's link to the work must be built by hand (§5.1).
2. **`StartBeforeStart(delay=...)` takes a constant.** A per-job overlap needs the
   delay to be a variable, so Rule 3's release is a hand-written linear constraint
   (§5.3).

---

## 5. The model

**Index sets.** Jobs `J` = Rule 1 batches (upstream, unchanged — never
re-consolidated). Tasks `T` = one per routing op per job. Machines `M`, machining
subset `M_c` (CNC/VMC). Shifts `S` = `(date, first|second)` over the horizon.
Operators `O`, each with a Settings machine list `Q(o)` and a fixed shift `σ(o)`
(rotation was removed 2026-08-05). All times are integer minutes from the plan
start floor.

### 5.1 Rule 1 — the roster

Variables, created only for `m ∈ Q(o) ∩ M_c`, shifts `s` with `shift(s) = σ(o)`,
and `o` not absent in `s`:

```
x[o,m,s] ∈ {0,1}                     o mans m for the whole of shift s
Σ_m x[o,m,s] ≤ 1        ∀ o, s       nobody hops mid-shift          (Rule 1)
staffed[m,s] = Σ_o x[o,m,s] ≤ 1      one person per machine per shift
```

Qualification is **exactly** the Settings machine list. Role is not a gate — that
regression cost the shop CNC4 sitting idle with work waiting (2026-08-07).

Linking the roster to the work is where Rule 2's *"may span an unmanned shift"*
clause bites, because a decision-dependent break is impossible (§4.2). Two
encodings, and the plan **measures before choosing**:

**E1 — dark-shift blocking (cheap, restrictive).** For each `m ∈ M_c` and shift
`s`, an optional interval spanning `s`, present iff `¬staffed[m,s]`, added to
`m`'s no-overlap. ≈ `|M_c| × |S|` ≈ 1,400 intervals; no per-task variables.
Cost: an operation may not *span* an unstaffed shift, so it must be staffed in
every shift it touches. That is a restriction beyond the four rules — Rule 2
*permits* holding a part across an unmanned shift, it does not require it. It errs
toward under-claiming capacity, so a plan built this way is always runnable.

**E2 — per-shift processing (exact).** For machining task `t`, machine `m`, and
each shift `s` inside `t`'s window:

```
w[t,m,s] ≥ 0                          minutes t processes on m during s
Σ_s w[t,m,s] = duration(t)            when t is assigned to m
w[t,m,s] ≤ overlap_min(t, s)          time-overlap of t's interval with s
w[t,m,s] ≤ len(s) · staffed[m,s]      no processing in an unstaffed shift
```

`overlap_min(t,s)` is itself a variable (`min(end_t, end_s) − max(start_t,
start_s)`, clipped at 0). Exact Rule 2, but ≈ 4 constraints per `(t, m, s)`
triple — tens of thousands even after window tightening.

~~**Decision: E2 is the target; E1 is a flagged fallback.** Task 1 of the
implementation plan is a measurement on the owner's real book — model size, solve
time, and **how often the restriction actually binds under E1**. If E1 never
binds, or binds at a cost under a late-day, E1 ships and E2 is recorded as
measured-and-not-worth-it. The flag is `cp_hold_across_unmanned_shift`, default
`True` (= E2). This is the one place the design deliberately leaves a measured
decision open; it is not left open in the code.~~

**SUPERSEDED BY THE MEASUREMENT — E1 SHIPS, and the flag default is `False`
(2026-08-14, owner-authorized).** The paragraph above is kept because it records
what was believed before Task 1 ran; the measurement is what changed it, and §5.1
above already says the outcome must be recorded here.

What Task 1 found on the owner's real book:

* **E2 does not scale.** From ~30 batches upward the model returns **no plan at
  all** within the worker's time budget. An encoding that answers "infeasible" on
  the owner's own book is not a target; it is unusable.
* **E1's restriction binds, and its cost is bounded and small.** The solver held a
  part across an unmanned shift in **3 of 116 operations** — the machine keeps the
  part while nobody is there, which the shop tolerates (the part sits in the
  chuck) and the decoder reproduces faithfully.

So `cp_hold_across_unmanned_shift` ships **default `False` (= E1)**. E2 remains in
the model behind the same flag — it is exact, and a future model that can afford
it should be able to switch back without re-deriving the encoding. The decision is
**not** left open in the code: `Config.cp_hold_across_unmanned_shift` defaults to
`False` and `engine/cp_adapter.CP_HOLD_ACROSS_UNMANNED_SHIFT` carries the same
value for the config-less path (`cp_engine.solve.solve_book`'s own default is
`True`, so the adapter passes it explicitly rather than relying on it).
`tests/test_cp_wiring.py` pins the shipping default.

### 5.2 Rule 2 — no segmentation

Free by construction, and cheaper than the rules that need work:

* a CP interval is contiguous, so an operation cannot be sliced;
* `allow_breaks=True` lets it span calendar breaks with the machine held;
* one task per operation, `optional=False` — **no parallel split**. Today's
  `flow_scheduler._try_split` behaviour is not modelled, because Rule 2 forbids it.

`allow_idle` stays `False` except under E2, where held-but-unstaffed time is idle.

### 5.3 Rule 3 — overlap in whole pieces, per job

~~`k_j ∈ {1 … qty_j}` is an integer decision per job: the pieces that must clear
before the successor may start, i.e. `k_j = ceil(p_j × qty_j)` with `p_j` free.~~
For consecutive in-house ops `a → b`:

```
start_b ≥ start_a + setup_a + k_j · cycle_a          release
start_b ≥ end_a   − (qty_j − k_j) · cycle_a          release, from the tail
end_b   ≥ end_a                                       pacing — b never finishes first
```

**SUPERSEDED — `k_j` IS PROVABLY ALWAYS 1 (measured Task 6, 2026-08-14).** The
struck text above is what was believed; this is what the model turned out to be,
and §3's table row ("`p_j` decided per job") is superseded by the same finding.

`k_j` appears **only** in the two lower bounds above, and the right-hand side of
each is monotonically increasing in `k_j`. A successor is never *obliged* to start
at its lower bound, so **every schedule legal at any `k_j` is also legal at
`k_j = 1`**: the feasible set at 1 contains every other, and no objective —
makespan, tardiness, or the fairness pair — can make `k_j > 1` strictly pay. The
earlier belief that a tardiness objective might make it a real choice (a job
holding a machine back for a more urgent one) is wrong: holding back is already
free.

So under this engine Rule 3 is, in effect, **"release after one piece" — maximum
overlap, always.** That is a real statement about the owner's overlap rule, not a
defect, and it is the reason there is genuinely nothing to sweep: this engine does
not tune a global overlap percentage the way the incumbent's contest did. Two
consequences the code depends on, both recorded here so a later reader does not
conclude overlap tuning is being handled:

* **there is no overlap knob under cp** (`optimizer.knob_for` → `(None, ())`,
  `optimize_service.cloud_candidates` → one job, `cp_adapter.sweep_optimize` → one
  solve). The *decision* is right; the reason is `k ≡ 1`, not "the solver tunes it".
* `k_j` is **under-determined above its lower bound** — being in no objective, any
  consistent value is equally optimal. CP-SAT's presolve fixes it to the lower
  bound today (measured over five seeds), so `cp_overlap_of` carries 1, but nothing
  in the model *requires* that. A decoder must read it as "at least this many
  pieces had cleared", never as "the release this schedule needs"; the drift check
  (§8) is what proves the replay matches the solve.

The domain `1 … min(pieces still owed by any step it governs)` is still pinned by
tests, because the domain is what fixes what `k` MEANS to a decoder.

OS and dispatch keep `end_before_start` in both directions (fully sequential), and
a step with no cycle time does not overlap — both unchanged from
`roster_engine/release.py`.

**Honest limitation, and how it is contained.** The engine's rule is worked
minutes, not wall-clock — *"an overnight gap must not release pieces that were never
cut"* (`release.py`). The two release bounds above are wall-clock and therefore an
**approximation**: breaks falling inside the predecessor make the first bound
optimistic and the second pessimistic, which is why both are imposed. The
**decoder (§7) computes the exact worked-minute release**, so the plan on screen is
exact; CP's version is a search-time approximation. The gap between them is
measured by the drift check (§8) rather than assumed small. If drift proves
material, the fix is to tighten the CP bounds, never to loosen the decoder.

### 5.4 Rule 4 — setup on change

Sequence-dependent setup is what killed the current model: 18,944 pairs, a 90 s
solve returning no feasible solution and a floor of 0 (`tardiness_bound.py:384`).
The encoding is inverted to make the *rare* case the modelled one.

Setup is charged **into the duration** — every machining mode is `90 + qty ×
cycle` — and a **credit** is granted only where Rule 4 says no setup is owed:

```
same(t1,t2)  ⇔  t1, t2 share (item_code, process_seq)
arc[m,t1,t2] ⇔  t2 runs directly after t1 on machine m   (sequence-var literal)
duration(t2 on m) = 90 + cutting(t2) − 90 · Σ_{t1: same} arc[m,t1,t2]
```

Pairs with identical `(item, process)` that could share a machine are few — sibling
batches of the same item — so the credit set is small even though the circuit
itself is not. The circuit is confined to `M_c`; manual and inspection machines owe
no setup and get no sequence variables. Whether that is enough is measured in the
same Task-1 spike as §5.1; the fallback is unconditional 90 minutes, which is
conservative (over-estimates duration, so the plan stays runnable) but forfeits the
same-part-back-to-back saving and is therefore **not** Rule 4 as written. It ships
only with the owner's say-so.

### 5.5 Frozen in-progress work

A frozen op pins **where and when, never how much** (2026-08-11). In the model:

* modes restricted to the pinned machine;
* `x[o,m,s] = 1` forced for the pinned operator in the shifts the op runs;
* `earliest_start` set from the previous plan; no setup charged on resume;
* quantity from **`Batch.process_remaining`**, never a per-SO-line remainder — the
  defect that left 281 pieces of a clubbed order in no plan at all.

Translation of `engine.freeze.compute_frozen_set`'s per-SO-line rows into batch-level
pins is the delicate part and is lifted from `roster_adapter._pins` unchanged,
including its one-`FrozenOp`-per-`(batch, op)` collapse.

---

## 6. The objective — total first, then fairness

Let `D_j` be the **integer days late** of order `j`, in the app's own unit:

```
1440 · D_j ≥ end_j − due_j ,   D_j ≥ 0
```

Minimisation drives `D_j` to its lower bound, so `D_j = ceil(...)` exactly, matching
the app's `(completion.date() − due_date).days`. Days, not minutes: it is the number
the owner is judged on, and it keeps the squares in §6.2 small (0–60 → 0–3600).

### 6.1 Phase 1 — efficiency

```
minimise  Σ_j D_j            total late-days
```

This is the number on the Schedule tab, and it is the primary objective. It matches
commit `b7beb18` (2026-08-13), which made the on-time term linear precisely so the
score tracks total late-days.

### 6.2 Phase 2 — fairness

```
subject to  Σ_j D_j ≤ T* + ε          T* = phase-1 result, ε default 0
minimise    Σ_j D_j²
```

**Why squared tardiness is the right measure here, and not an arbitrary pick.**
With `Σ D_j` held fixed by the phase-1 constraint,

```
Var(D) = (Σ D_j²)/n − ((Σ D_j)/n)²
```

and the second term is now a constant. Therefore **minimising `Σ D_j²` is exactly
minimising the variance of tardiness across late orders** — the most even
distribution achievable at the best total. The owner's example falls out directly:
ten orders 10 days late scores 1,000; nine orders 2 days plus one order 82 days
scores 6,760 at the identical total, and the concentrated plan is rejected.

`Σ D²` is preferred to `max D` (which pyjobshop offers natively as
`weight_max_tardiness`) because max stops discriminating once the worst order is
pinned, while `Σ D²` keeps spreading everything below it. It is preferred to a
blended `Σ D + λ·max D` because the trade-off would live in an untunable λ rather
than in a stated rule.

**`ε` defaults to 0**, so fairness can never cost a single late-day — it decides
only where phase 1 was indifferent, which is a strict tie-break and leaves
`b7beb18` intact. `ε` exists as a config knob (`cp_fairness_slack_days`) for the
day the owner decides a couple of late-days is worth cutting the worst order; that
is then a config change, not a redesign.

### 6.3 Encoding

`D_j²` enters as **exact integer squares via linear lower-bounding lines**, not
`add_multiplication_equality`:

```
DSQ_j ≥ (2k+1) · D_j − k(k+1)        for k = 0 … cap
```

At integer `D_j` the tightest line is exactly `D_j²`, and minimisation selects it.
This keeps the model linear, which CP-SAT propagates far better than a quadratic
term. ≈ 60 constraints per order, ≈ 4,000 on a 68-order book — negligible.

Phase 2 is a **second solve** warm-started from phase 1's solution
(`Model.solve(initial_solution=...)`), with `Σ D_j ≤ T* + ε` added. A single-solve
big-M form (`minimise 250000·ΣD + ΣD²`, exact because `ΣD² ≤ 68 × 3600 <
250000`) is the fallback if the second solve proves not worth its time; it was
confirmed working in the §4.1 smoke test. Two-phase is preferred because it
survives a phase-1 that hits the time limit without proving optimality: `T*` is
then simply the best found, and the tie-break still applies.

### 6.4 What happens to `optimizer.score`

`engine/optimizer.score` stops driving anything and becomes a **reported number**.
It is symmetric — it penalises finishing far *early* as hard as finishing late — and
`tardiness_bound.py:683` already documents a case where that made the app correctly
reject a CP plan 86 late-days better. With CP as the optimizer, the Apply screen
shows **both** yardsticks side by side: total late-days and spread (the CP
objective), and `optimizer.score` beside them. Neither is hidden, and the gate
(§9) is explicit about which one it ranks on.

The worst-order ceiling (`worst_ceiling_days`, weight 100) is **dropped from the
objective** — phase 2 subsumes it, since minimising `Σ D²` caps the worst order
harder than a penalty on breaching a threshold — but is **kept as an Apply-time
no-regression check**, which is the job it was actually doing.

---

## 7. The genome, and the fast replay

The solve runs off-box. `/run` must answer a page load in seconds, so it **replays
stored decisions**, exactly as it replays applied ranks today.

`_optimize_apply` (`api/main.py:2325`) already persists a decision genome —
`ranks`, `best_overlap`, `crew_rank`, `flexible_machines` — into an arbitrary `meta`
dict, and every later `/run` replays it. CP needs a richer genome, not new
plumbing:

| Key | Meaning | Status |
| --- | --- | --- |
| `ranks` | job order, `"<so>\x1f<item>"` → rank | exists |
| `cp_machine_of` | `(batch, op_seq)` → machine id | new |
| `cp_roster` | `(machine, date, shift)` → operator | new |
| `cp_overlap_of` | batch → `k_j` pieces | new |
| `cp_completion` | batch → the solved completion date | new — the drift check's baseline |
| `cp_solved_book_sig` | the book the solve saw | new |

Every existing reader uses `meta.get()`, so older code ignores the new keys and a
rollback needs no migration.

**`cp_engine/decode.py`** takes the genome plus today's book and lays out times:
machine fixed by `cp_machine_of`, operator fixed by `cp_roster`, order fixed by
`ranks`, release computed **exactly** in worked minutes from `cp_overlap_of`,
frozen ops pinned. It is a simulator with every decision removed — which is what
`roster_engine/scheduler.py`'s shift clock already is, minus its choices, so that
layout code is adapted rather than rewritten.

Orders CP never saw (uploaded since the solve) are appended in routing order after
the ranked work and flagged. That is the existing
`optimize_meta.dates_changed` / "run Start deep search" banner, reused.

---

## 8. The integrity check

CP computes times; the decoder recomputes them. **If those disagree, the plan on
screen is not the plan that was solved.** This repo has paid for that class of
defect repeatedly — the Gantt saying 07-Sep while the delay report said 04-Sep, four
reporting features rebuilding the shop's working hours and disagreeing with the
engine by 9,470 minutes.

So it is checked, not assumed. `cp_engine/report.py::completion_drift(entries,
genome)` compares each order's replayed completion against `cp_completion`:

* **on the book that was solved, drift must be 0** — asserted in tests, and a
  loud non-blocking report row live;
* after the book moves, drift is expected and the existing staleness banner covers it.

It is appended by `api._report_for_book` alongside `routing_order_violations`,
`qualification_violations` and `batch_quantity_violations` — non-blocking, because a
live plan must never break.

The four roster rule checks (`OPERATOR_SPLIT_SHIFT`, `OPERATION_SEGMENTED`,
`MACHINE_DOUBLE_BOOKED`, `IDLE_CAPACITY`) in `roster_engine/report.py` are **reused
unchanged** against the CP plan. They are an independent implementation of the four
rules, written for a different engine, which is exactly what makes them worth
running: they must all be 0, and a non-zero is a model bug, not a rule exception.

---

## 9. Wiring

Seven dispatch sites branch on `config.scheduler`; the roster plan's §W.1 enumerates
them and **all seven must learn `"cp"` in one commit**, or the plan and the search
run different engines while every screen still looks green.

| # | Site | Becomes |
| --- | --- | --- |
| 1 | `pipeline.scheduler_for` | `"cp"` → `cp_adapter.run` (the replay) |
| 2 | `optimizer.optimize` | `"cp"` → `cp_adapter.solve` (the CP solve) |
| 3 | `optimizer.sweep_optimize` | `"cp"` → `cp_adapter.solve` — there is no sweep; overlap is inside the model |
| 4 | `optimizer.knob_for` | `"cp"` → no knob (returns `None`) |
| 5 | `optimize_service.cloud_candidates` | `"cp"` → one job, not a candidate grid |
| 6 | `optimize_service.run_candidate` | `"cp"` → run the solve, return the genome |
| 7 | `optimize_service.contest_jobs` | `"cp"` → `(False,)`; machine choice is in the model |

`_inputs_signature` folds in `cp_engine.SCHEDULER_FINGERPRINT` — without it, a
genome solved under one version replays under a changed one behind a green "up to
date" banner.

**Delivery: CP proposes, the owner applies.** The solve runs where the contest
already runs (GitHub Actions / the Oracle worker), lands as a candidate in the
Optimize panel, and reaches the floor only through the existing Apply gate —
including its committed-promise backstop, which is untouched. The auto-apply path
(`_auto_apply_result`) ranks on **total late-days, then spread**, with
`optimizer.score` displayed but not decisive (§6.4), and keeps the worst-order
no-regression check.

**Retired, not deleted.** `roster_engine/search.py` and `roster_engine/roster.py`
stop being reachable from the live path but stay green, exactly as classic, flow and
new did. `DEFAULT_SCHEDULER=roster` remains a one-env-var rollback.
`scripts/tardiness_bound.py` keeps working and gains a second life: with the four
rules now enforced it stops being a floor-on-a-floor, so it is re-pointed at
`cp_engine` and reports the **true** optimality gap.

---

## 10. Risks

| Risk | Containment |
| --- | --- |
| **Tractability.** Rule 1 and Rule 4 both cost what the mode-count collapse saves; the current model already died once at 90 s (exit 143 on a runner) | Task 1 is a measurement on the owner's real book before any integration. E1/§5.1 and unconditional setup/§5.4 are named, costed fallbacks — each forfeits something specific, and neither ships silently |
| **Release approximation** (§5.3) — CP's wall-clock bounds vs the decoder's worked minutes | The decoder is exact; drift is measured (§8), not assumed. Fix by tightening CP, never by loosening the decoder |
| **pyjobshop upgrade** breaks the escape hatch — `CPModel`, `.variables`, `assign_vars` are internal API | `tests/test_cp_escape_hatch.py` is the canary and fails loudly. pyjobshop is pinned; it is a worker-only dependency and never reaches Render |
| **Solve returns nothing** in the time limit | The genome is only replaced on Apply. No result → the floor keeps the plan it has, and the note says so |
| **A better total, concentrated on one customer** — phase 1 can do this before phase 2 sees it | Phase 2 fixes it at zero cost in late-days. The Apply screen shows worst-order and spread next to the total, so a concentrated plan is visible before it is applied |
| **Two definitions of the schedule** (model and decoder) | §8. This is the defect class the repo keeps paying for, so it gets a check rather than a convention |

---

## 11. Out of scope

* **Re-consolidation.** Rule 1 clubbing (`rule1_consolidate`) stays upstream and is
  never re-derived. `ppc_engine/consolidation.py` is known-broken and unreachable.
* **The commitment feature.** `COMMITMENT_FEATURE_ENABLED = False`; the promise
  penalty has an empty input map and contributes 0. If it is switched back on it
  belongs in the objective as a constraint on `D_j`, not as a weight.
* **Earliness.** Dropped with `optimizer.score`'s demotion. The owner's objective is
  late deliveries; a symmetric term is what made the app reject an 86-late-day
  improvement.
* **Solving on Render.** The free instance cannot run CP-SAT. The solve is
  worker-only, always.
