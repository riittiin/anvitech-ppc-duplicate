# Admin owns operator shifts: remove the Friday rotation (2026-08-05)

## The bug that triggered this

Anvitech's director opened the downloaded shift-wise schedule and saw **Sidhu
Singe on 1st shift in week 1 and 2nd shift in week 2**, even though his "Stays"
pin was ticked.

**Root cause, proven, not inferred.** The pin never reached the planner:

- `engine/new_engine.py:133` builds the engine's operator from four fields only.
  The engine's `Operator` (`ppc_engine/domain/resources.py:93-109`) has
  `['base_shift', 'name', 'qualified_machines', 'role']`. **There is no pin.**
- `ppc_engine/worktime.py:117-126` `_shift_for` then flips every `Role.OPERATOR`
  person on every odd Friday since `week_anchor`, unconditionally.

Reproduced end to end: with `shift='First shift', pinned=True` stored, the engine
returned FIRST, SECOND, FIRST, SECOND, FIRST, SECOND on successive weeks.

So the pin only ever controlled the Settings display and the person's *starting*
shift. The planner re-derived the rotation itself and flipped them anyway. The
tick box has been decorative since the new engine went live.

The same boundary would swallow a manually set shift, which is why the fix has to
happen inside the engine and not only in the app.

## Decision (owner, 2026-08-05)

**Remove the Friday rotation completely.** The shift set in Settings is the shift
the planner uses, every week, until an admin changes it. The "Stays" pin is
abolished because with nobody rotating it means nothing.

Rejected alternatives: making the pin work (keeps the thing that just misled a
director, and Settings still would not match the plan for unpinned people); and
adding a manual "swap all shifts" button (extra surface nobody asked for; can be
added later if editing rows proves tedious).

## Design

### 1. The engine stops rotating

`engine/new_engine._plan_config` currently sets
`week_anchor=_friday_on_or_before(start)`. It will pass **`week_anchor=None`**.

This uses a path the engine already supports rather than new logic:
`ppc_engine/worktime.py:121-122` returns `base_shift` unchanged when the anchor is
`None`. `ppc_engine` is not modified at all.

`_friday_on_or_before` becomes unused by `_plan_config`; leave the helper (it is
also the app's `last_friday` idiom) but remove the call.

### 2. The app stops rotating its stored table

`engine/operator_master.rotate_table` returns the table unchanged. Whatever is
stored is what is displayed and what is planned. `operators_as_of` therefore
returns the stored shifts as-is for any date.

`next_rotation` is no longer meaningful. `GET /operators` keeps returning the key
(so the frontend contract does not break) but the UI stops rendering it; the
value becomes `None`.

### 3. Analytics stops rotating too

`engine/analytics.py:237` derives operator capacity using its own rotation anchor.
Left alone it would compute a person's available hours against the wrong shift
once the plan crosses a Friday. It must use the stored shift for every day.

**This is the easy one to miss.** It is a second, independent copy of the rotation
rule, and the per-operator "how busy" numbers are wrong without it.

### 4. Settings UI

- The **Stays** column and its checkbox are removed from the operators table
  (both the admin and the read-only role render).
- The "Next rotation: Friday DD-MM-YYYY" line is removed.
- The Shift dropdown stays and becomes the single control over who works when.
- The panel explainer currently reads "Shifts swap every Friday", written
  2026-08-05 and now false. It becomes something like: "Who runs which machine,
  and on which shift. The shift you set here is used every week until you change
  it."

### 5. Stored fields stay, dormant

`pinned` on each operator row and `week_anchor` on the table remain in the store
and in the `POST`/`PATCH /operators` request models, so **no migration is needed**
and nothing 500s on an existing store. Nothing reads them for scheduling. This
mirrors how the commit/uncommit lanes were retired on 2026-08-04.

### 6. The staleness banner needs no new machinery

`api/main._inputs_signature` already folds each operator's **shift** into the
fingerprint (name/machines/shift/pin, ids and `week_anchor` excluded). So editing
a shift already flips `optimize_meta.inputs_changed` and raises the existing
"settings changed, run Start deep search again" warning.

This is to be **verified end to end**, not assumed: change a shift, re-plan,
confirm the banner appears; revert, confirm it clears.

## Consequences the owner accepted

**Current shifts become permanent on deploy.** Whatever each operator's shift says
in Settings that day is what they work until edited. Some rows may currently show
a rotated shift rather than the intended one, so the operator list is worth a
review before deploying.

**Tests will need updating, correctly.** Ten-plus test files encode the rotation
as a requirement (`test_operator_master.py`, `test_operators_api.py`,
`test_analytics.py`, `test_pipeline_golden.py`, and others). They are not wrong
today; they test behaviour being deliberately removed. Each is updated to assert
the new rule (shift is stable across Fridays) or deleted where it existed only to
prove rotation. **No test may be weakened merely to make it pass** — if one fails
for a reason other than the removed rotation, that is a real regression and stops
the work.

## Testing

- **Engine**: an operator's shift is identical on both sides of a Friday, over a
  multi-week horizon. This is the direct regression for the reported bug.
- **App**: `rotate_table` returns its input unchanged, including across several
  elapsed Fridays; `operators_as_of` returns stored shifts for any date.
- **Analytics**: a plan spanning a Friday computes each operator's capacity
  against their stored shift.
- **API**: `GET /operators` still succeeds against a store containing the old
  `pinned`/`week_anchor` fields; `PATCH` with `pinned` still returns 200 and does
  not error.
- **Banner**: changing a shift flips `inputs_changed`; reverting clears it.
- **Browser**: no Stays column, no "Next rotation" line, shift dropdown works,
  and the operators panel still renders for the read-only role.
- **Full suite green**, and the golden trace must move only if the rotation
  genuinely affected it, in which case the change is regenerated deliberately and
  the reason recorded.
