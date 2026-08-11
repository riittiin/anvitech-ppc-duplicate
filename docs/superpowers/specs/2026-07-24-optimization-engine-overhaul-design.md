# Optimization Engine Overhaul — Design & Autonomous Loop Plan

**Date:** 2026-07-24
**Status:** In progress — owner delegated full authority ("loop engineering", human-out-of-the-loop).
**Owner mandate:** Build the best optimization engine considering ALL parameters. Try many
formulations/combinations, fabricate test books, self-judge results, converge on the best.
Take hours if needed. Owner is NOT in the iteration loop — Claude judges goodness.

## Why (history)
- v1 objective minimized total delay only → pushed a promised order Aug-8→Aug-23 (a single
  order abandoned). Fixed with a convex per-order guard + worst-order ceiling (shipped
  2026-07-24, commit range …136a327).
- v2 (current shipped) still surprised the owner: an "optimized" plan showed 55/57 late vs
  39/57 standard. Root finding: the panel/objective use raw late-COUNT and total late-days,
  which hide the lateness DISTRIBUTION. Measured (Test5): the optimized plan actually pulled
  10 orders OUT of the 20+ day "very bad" bucket (23→13) and cut total late-days 866→745 —
  a genuine improvement by the owner's value model, invisible in the panel.
- Owner's value model (explicit): an order 2–4 days late is FINE; 10+ days is NOT fine.
  So the right measure is a convex per-order cost by how-late, not a raw count.

## The Judge (fixed definition of "good" — owner's values)
Plan cost `J` (lower is better), minimized and used to rank every candidate plan:

```
J = Σ_orders  order_cost(Lᵢ)                     # Lᵢ = days late (0 if on-time)
  + w_make · makespan_days
  − w_util  · utilization_score                  # reward high, balanced machine+operator use
  (+ ∞ if the worst order exceeds the incumbent's worst — the no-regression ceiling)

order_cost(L) = 0                         if L ≤ TOL           # TOL ≈ 4 days ("fine")
              = A·(L−TOL) + B·(L−TOL)²    if L > TOL           # convex ramp
              + STEP10  if L ≥ 10                              # "bad" step
              + STEP20  if L ≥ 20                              # "very bad" step
```

Owner-anchored constants of the JUDGE (fixed; reflect the owner's bands): TOL=4, bad=10,
very-bad=20, with STEP10/STEP20 large enough that a 10+ or 20+ order dominates many small
slips. `w_util` rewards utilization (per-machine + per-operator, penalizing imbalance).
The ENGINE's own objective weights are what get SEARCHED to best satisfy J across books —
J itself is not tuned (avoids circularity: J = the owner's values; the engine is tuned to J).

`utilization_score`: mean machine utilization + mean operator utilization − a spread/imbalance
penalty (so "everyone reasonably busy" beats "a few maxed, most idle"). Uses the fixed
analytics capacity (physical windows, post the 2026-07-24 manual-station fix).

## Levers searched (the engine, not the judge)
- **Objective shape/weights** the search minimizes (proxy for J): per-order convex tardiness
  (tolerance, linear+quadratic coefficients, cap), late-band step penalties, makespan weight,
  worst-order ceiling weight, utilization term.
- **Job sequence** (the sequence search — already exists; retune under the new objective).
- **Overlap %** (auto-tuned — keep).
- **Consolidation window** (auto-tuned).
- **Operator-pick policy** (scarce/balanced/flexible — auto-decide the best).
- **Staffing rule (NEW):** one operator per machine per shift; exception when a job's duration
  on that machine is short (< short_job_threshold). Tune the threshold.

## Hard constraints
- One operator per machine per shift (short-job exception).
- Worst order never increases on a re-optimize (ceiling + apply backstop — keep).
- Absences, machine qualifications, calendar (Thu/holiday off), setup on CNC/VMC only.

## Product changes (owner-requested)
- **Remove the "Order Priority" setting** from Settings/Schedule — the engine decides the
  sequence; the UI no longer exposes it.
- Optimize panel: show the lateness DISTRIBUTION (severity bands) for standard vs optimized,
  plus the judge score, so a genuinely-better plan reads as better (no more scary raw count).
- Utilization is a first-class goal (maximize, balanced) — surfaced and optimized.

## Method (the loop)
1. Judge + fabricator (diverse books: due-date tightness, order count, CNC-heavy vs
   manual-heavy, feasible vs impossible) + baseline the current engine.
2. Sweep objective formulations × coefficients × sequencing × staffing/operator-pick;
   score every plan with J; keep the winner; log the frontier.
3. Implement the staffing rule + utilization; measure.
4. Converge; stress-test on edge cases + the real book (Test5).
5. Implement into the engine (TDD), remove Order Priority, update panel, full suite.
6. Report with numbers.

## Progress log
- (this doc created) — parameters + judge defined; harness next.
- **Harness built** (scratch/overhaul/): judge + plan-runner (distribution + machine/operator
  util) + book fabricator (Test5-based variants: real/tight/loose/small).
- **Baseline (current engine, judged):** the optimizer improves the tardiness DISTRIBUTION
  (real book: 20+ orders 23→13, bad(10+) 32→28, J 12786→8678) but consistently SACRIFICES
  (a) worst-order (42→51, ceiling is app-layer only, not in the raw search), (b) makespan
  (45→48), (c) utilization (67.5→64.6). On the overloaded "tight" book it barely helps and
  worsens the worst (62→71). ⇒ objective must fold in worst-order + makespan + utilization,
  not just tardiness.
- **STAFFING RULE ALREADY EXISTS:** `ppc_engine/scheduler/staffing.py` enforces one operator
  per machine per shift BY CONSTRUCTION (assignments keyed by (machine, date, shift); a person
  mans ≤1 machine/shift; decoder asks per-shift only). Owner's rule is the engine's foundation.
  Remaining: the SHORT-JOB EXCEPTION (let an operator cover a 2nd machine when a job is tiny)
  as a utilization lever — to evaluate, not assumed beneficial.
- **Order Priority UI** = `web/index.html:171` fieldset (`#cfg-priority-window` →
  `priority_window_days`) + `web/app.js` readConfig. To remove; engine auto-decides the value.
- **Objective sweep DONE — objective is NOT the lever.** All 5 formulations landed within 0.3%
  of the current engine (best F4 33958 vs current 33863). The sequence search is at its ceiling;
  objective shape barely moves J. (Matches prior codebase research: "sequencing converged.")
- **Structural sweep DONE — consolidation is the big lever.** operator_pick × consolidation on
  real+tight: consolidation **10d (current default) is the WORST**; dropping to ≤3d is a ~5-6%
  J win (best: balanced+≤3d = 31046 vs current scarce+10d = 32905). consolidation 0/1/3 tie
  (few same-item orders within 3d). operator_pick: balanced ≈ scarce overall; scarce gives
  FEWER bad(10+) orders on the real book, balanced wins the overloaded book. Matches the
  codebase note "consolidation window 1 day is best" — but the shipped DEFAULT is 10.
- **Order Priority UI** = `web/index.html:170-193` fieldset (metric `#cfg-priority-metric` +
  window `#cfg-priority-window`). Remove entirely; engine uses recommended defaults (slack
  metric, no window limit). Overlap is already auto-tuned + shown read-only (the pattern).

## Converging implementation plan (pending final confirmation sweep)
1. **Consolidation default 10 → 1** — measured ~5% win; matches codebase memory. (config.py)
2. **Objective: modest explicit 10/20 band-step penalties** (judge-align) — validated J-neutral;
   makes "a bad order" an explicit cost so the objective reflects the owner's values. (ppc
   objective + engine/optimizer mirror)
3. **Optimize panel: show the lateness DISTRIBUTION** (on-time / 1-4 / 5-9 / 10-19 / 20+) +
   worst-order for standard vs optimized, so a genuinely-better plan reads as better (kills the
   "55 late" false alarm). (backend optimize result + frontend)
4. **Remove the "Order priority" fieldset**; engine decides (slack + no-limit). (index.html/app.js/config)
5. operator_pick: keep scarce (fewer bad orders on the real book) unless confirmation says otherwise.
6. Short-job staffing exception: DEFERRED — engine already enforces one-op-per-machine-per-shift;
   the exception is a big change with unproven payoff. Revisit if utilization stays low.

## KEY INSIGHT (2026-07-24) — the worst-vs-distribution conflict was a broken metric
Diagnosis on the real book (per-order lateness across consol-10 / consol-1 / consol-1+ceiling):
- **24 of 57 orders are DOOMED** — ≥10 days late in EVERY plan (structurally impossible from a
  mid-cycle start; the shop is over capacity for this book). The top-5 worst are all doomed
  (best-case 41/51/46/28/31 days late). 33 orders are savable.
- **The raw-worst-order ceiling was SACRIFICING savable orders for a doomed one:** to hold the
  doomed SO82 at 46 vs 63, the ceiling pushed **7 SAVABLE orders** (SO123/124/139/140/69/74/95,
  all 6-9 days late) into badly-late (10-14 days). bad(10+) 25 → 32.
- ⇒ "worst never increases" (raw number) and "fewest badly-late" only conflict because the raw
  metric conflates DOOMED and SAVABLE orders. They are NOT really in conflict.

**Resolution (satisfies both — owner-endorsed direction "make it satisfy both"):**
- **The Judge is the single arbiter** (objective + apply gate). Its convex+step per-order cost
  makes pushing a SAVABLE order into "bad" very expensive (protects the Aug-8 case) while a
  DOOMED order drifting is nearly free (no wasted distortion). REPLACES the raw-worst ceiling
  (`worst_ceiling_days` + the `_auto_apply_result` max-late backstop shipped on main 2026-07-24).
- Optional hard floor for owner certainty: an order that is ON-TIME in the incumbent may not be
  pushed to badly-late (≥10) by a re-optimize — the literal Aug-8 guarantee, compatible with the
  Judge.
- **Surface the doomed set** in the panel: "N orders cannot be delivered on time regardless
  (over capacity) — [list]", so the owner can outsource / add capacity / renegotiate those.
- This means REVERTING the raw-worst ceiling machinery (`worst_ceiling_days`, ceiling barrier,
  max-late apply backstop) in favor of the Judge + savable-protection + doomed surfacing.

## Implementation status (branch optimization-engine-overhaul)
- **Task 1 DONE** (commit 70b6065): consolidation engine-decided (forced 1 at
  `_resolve_config`, normalized in the signature, removed from Settings). Classic default
  stays 10 so golden/rule1 untouched. 553 pass. The measured ~6% Judge win.
- **Task 5 DONE** (commit f40a060): "Order priority" setting removed; engine uses the
  recommended PRIORITY_SLACK + no-window defaults. 553 pass.
- **REMAINING:** Task 4 (surface the lateness distribution + doomed-order set in the panel —
  the trust fix), Task 3 (replace the raw-worst ceiling with the Judge + savable-order guard),
  Task 2 (explicit 4/10/20 band steps in the objective). Task 6 (final validation).

## DEEP EXPERIMENTATION (2026-07-24, owner pushed to experiment harder — rigorous this time)
- **Search budget IS a modest lever:** budget 150→800 dropped J 8339→7751 (fewer very-bad,
  higher util). Production uses 200 local / thousands cloud, so real runs get more.
- **Packing/makespan weight does NOT lift utilization:** swept the search makespan weight 4→400,
  machine util stayed 31-34% while J worsened. Low util is STRUCTURAL, not sequencing.
- **Capacity truth (per-order SOLO floor, scheduled alone):** only **16** of 57 orders are TRULY
  impossible (≥10 late even alone); **10** are deliverable-solo but wrecked in the plan (SO120
  on-time solo → 27 late). Machine mean util **31%**, operator **53%** (some at 94%).
- **Short-job exception BUILT + shipped** (commit): interval-based operator booking, feasibility-
  proven (0 double-bookings), 21 operator-shifts cover >1 machine. Impact on the real book:
  **roughly neutral** (worst 63→53, total-days 734→694, but bad 25→30). The freed operators
  aren't the binding constraint — idle machines need specific qualifications/ready-work they
  can't supply. Kept (owner-requested, correct, helps other books).
- **Scheduler head-to-head (naive plan):** new(ppc) J=12324 BEATS flow 12915 and classic 23254
  on every axis. The current engine is the best scheduler available — no better one to swap in.
- **VERDICT (rigorous):** the objective AND the scheduler are near the CREW-limited frontier.
  Machines are ~30% idle because the shop is over CREW capacity for this book (16 orders
  literally impossible; machines idle for lack of qualified operators, not scheduling). The real
  win found = consolidation 10→1 (~6%, shipped). The remaining ENGINE value is TRUST/HONESTY
  (show the distribution + surface the 16 impossible orders for business action — outsource / add
  crew / renegotiate) + protecting deliverable orders from being sacrificed (savable guard).
  "More on-time" beyond this is a BUSINESS lever (crew/qualifications), not a software one.

## FINAL STATE (branch optimization-engine-overhaul, 2026-07-25) — SHIPPED to the branch
Owner decisions after seeing per-order numbers:
- **Consolidation 10→1** (engine-decided) — the ~6% measured win. [commit]
- **"Order priority" setting removed** — engine decides. [commit]
- **Short-job staffing exception** — interval-based operator booking; feasible (0 double-books),
  used (21 multi-machine shifts); ~neutral on this book but owner-requested + helps others. [commit]
- **Severity cap 30→60 ("distribute the pain")** — owner's explicit choice with numbers in hand:
  spread unavoidable lateness (worst 53→40; doomed SO108 53→26, SO107 47→23) accepting ~15 orders
  sliding on-time→5-13d. The single-worst-order ceiling is KEPT (owner: worst still protected). [commit]
- **Optimize panel now shows the lateness DISTRIBUTION + the impossible-order list** (15 orders
  20+ days late on the real book) — the trust + actionable-capacity fix. [commit]
- Objective band-steps (old Task 2) + raw-ceiling removal (old Task 3): SUPERSEDED — the cap change
  delivers the owner's shape and the owner chose to KEEP the worst-order ceiling.
- **Verdict:** the engine is at the crew-limited frontier; the real 'more on-time' lever is crew
  capacity, which the panel now surfaces (the 15 impossible orders). 557 pass, golden intact.
