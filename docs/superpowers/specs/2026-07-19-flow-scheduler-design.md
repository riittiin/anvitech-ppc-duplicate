# Flow scheduler (Sched2 productization) — design

**Date:** 2026-07-19 · **Owner mandate:** "72 days is unacceptable — break every
rule except the basics, rebuild from scratch." Research (same day) produced a
from-scratch scheduler measuring **43.7 days / 1169 late-days** on the live
71-order book (live applied plan: 70.8 d / 1460), verified by six independent
validators. This spec productizes it. Research artifacts:
`~/.claude/projects/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/research-v2-2026-07-19/`.

## The model (what changed vs Rule 6)

Kept (the owner's three basics): every working minute has the machine AND a
qualified operator; operators only within their own shift (1st 08-19, 2nd
19-05, day-window people 09-18; Thursdays/holidays off); process order per the
routing, piece-wise.

Broken (the two big ones, measured worth ~27 days together):

1. **No resource-holding.** Rule 6 seized machine+operator at op start and held
   them to the paced end while parts trickled in (overlap pacing). The flow
   scheduler occupies resources only while cutting pieces that already exist; a
   starved step RELEASES the machine and resumes when more parts arrive.
2. **Chunked piece-flow.** A batch moves between steps in `qty / flow_chunks`
   chunks (4 measured best); a successor step starts when the first chunk has
   cleared its predecessor, and chunks of one step may run CONCURRENTLY on
   alternative machines. Causality: pieces run at step k+1 only after they
   exist at step k (validator-enforced).

Setup (90 min, CNC/VMC only) is charged **per machine re-engagement**: taking
up an op the machine wasn't just running costs a fresh setup; consecutive
chunks of the same op cost one. (More honest than Rule 6's one-per-step — the
43.7 d plan pays 236 setups where Rule 6 would count 171.) Operator pick is
scarce-first (same rule shipped earlier today). OS steps: whole batch must
clear the predecessor, then a flat 24×7 vendor block, unlimited parallel.
DISPATCH/off-machine: zero-duration milestone at the latest batch end.

## Productization shape (smallest blast radius)

- **`engine/flow_scheduler.py`** — pure `run(prioritized_batches, config,
  masters, reserved=None) -> list[ScheduleEntry]`, same contract as
  `rule6_allocate.run`. Emits real `ScheduleEntry` objects (op_segments per
  shift, so Gantt/analytics/shift-wise/CSV all work unchanged), including
  DISPATCH/off-machine milestones for view parity. Honors `process_qty`
  (per-process remaining: pieces already punched through step k-1 are initial
  WIP for step k) and `reserved=` operator absences. Guard exhaustion raises
  `RuleError` — fail loud, never under-schedule.
- **Dispatch by config:** `Config.scheduler: "classic" | "flow"` (engine
  default **classic** — golden trace and every existing plan byte-identical;
  the LIVE saved config is switched to flow at cutover, like consolidation=1
  was). `Config.flow_chunks` (default 4). Neither appears in Settings — the
  owner's no-knobs rule; the optimizer owns chunk tuning.
- **One dispatcher** (`pipeline.scheduler_run(seq, config, masters,
  reserved)`) used by `run_forward` AND `optimizer.evaluate`, so search and
  replay always use the same scheduler as the plan.
- **Contest dimension:** in flow mode the settings sweep tunes `flow_chunks`
  over (1, 2, 3, 4, 6) instead of overlap (overlap has no meaning without
  pacing); classic mode keeps the overlap contest. Same fair-contest contract
  (equal depth, current setting first, wins ties). Cloud worker needs no
  change beyond the config fields riding the existing payload.
- **Staleness:** `SCHEDULER_FINGERPRINT` bumps to `flow-v1` when the flow path
  changes; the config's `scheduler` field is part of `_inputs_signature`
  already (it hashes the whole config), so the cutover itself flags applied
  ranks stale — correct, a fresh contest is required anyway.

## Cutover (after merge + owner's push)

1. Deploy; verify logged-in.
2. Save the live config with `scheduler: "flow"` (persist via /run).
3. Run Optimize (cloud contest, chunk candidates) → Apply.
4. Expected live plan class: **~44 days / ~1200 late-days** (vs 70.8/1460 now).

## Non-goals

- No UI changes beyond nothing-breaking (no new knobs).
- Rule 6 classic stays in the codebase (golden + fallback + comparison).
- The certified floor (37.2 d) and the night-crew cross-training lever are
  business facts, not code.
