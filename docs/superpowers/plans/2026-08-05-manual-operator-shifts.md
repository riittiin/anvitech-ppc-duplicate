# Admin-Owned Operator Shifts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The shift an admin sets for an operator in Settings is the shift the planner uses, every week, until an admin changes it. The automatic Friday rotation and the "Stays" pin are removed.

**Architecture:** The rotation exists in three independent places: the engine (`ppc_engine` rotates from a `week_anchor`), the app's stored table (`operator_master.rotate_table` flips rows), and analytics (its own rotation-aware capacity). All three stop rotating. `ppc_engine` itself is NOT modified: it already returns the base shift unchanged when the anchor is `None`, so the adapter simply stops supplying an anchor.

**Tech Stack:** Python 3, FastAPI, plain HTML/JS frontend, pytest.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-05-manual-operator-shifts-design.md`. Read it before Task 1.
- **Do NOT modify anything under `ppc_engine/`.** The engine already supports "no rotation" via `week_anchor=None` (`ppc_engine/worktime.py:121-122`). Use that path.
- **`rotate_table` returns a TUPLE `(new_table, flips_applied)`, not a dict.** A stub returning a bare dict breaks 154 tests. Keep the tuple shape.
- **Stored fields stay.** `pinned` on each operator row and `week_anchor` on the table remain in the store and in the API request models, so no migration is needed and an existing store never 500s. Nothing may read them for scheduling.
- **No test may be weakened merely to make it pass.** Tests that exist only to prove rotation are deleted or rewritten to assert the new rule. If a test fails for any reason OTHER than the removed rotation, that is a real regression: stop and report it.
- Use `python3`, not `python` — `python` is not on PATH on this machine.
- **Baseline: 758 passing, 1 skipped.** Exactly 9 tests are expected to fail from the rotation removal, in 3 files: 7 `test_rotate_*` in `tests/test_operator_master.py`, `test_prepare_contest_rotates_as_of_effective_start` in `tests/test_operator_wiring.py`, and `test_plan_config_floor_moves_clock_not_rotation_anchor` in `tests/test_plan_start_next_hour.py`. Anything beyond those 9 is a regression.

---

### Task 1: The engine stops rotating

**Files:**
- Modify: `engine/new_engine.py:196`
- Test: `tests/test_operator_wiring.py`, `tests/test_plan_start_next_hour.py`

**Interfaces:**
- Consumes: nothing
- Produces: `new_engine._plan_config(config).week_anchor` is always `None`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_operator_wiring.py`. This is the direct regression for the reported bug (a pinned operator rotated anyway), so it asserts on the ENGINE's own answer:

```python
def test_operator_shift_never_changes_across_a_friday():
    """The bug the director saw: an operator on 1st shift in week 1 showed on 2nd
    shift in week 2. The shift an admin sets must now hold for every week."""
    import datetime
    from engine.new_engine import _plan_config
    from engine.config import Config
    from ppc_engine.domain.resources import Operator as EngOp, Role
    from ppc_engine.domain.shift import Shift
    from ppc_engine.worktime import effective_shift

    start = datetime.date(2026, 8, 5)          # a Wednesday
    cfg = _plan_config(Config(plan_start_date=start))
    op = EngOp(name="Sidhu Singe", role=Role.OPERATOR,
               qualified_machines=frozenset({"CNC1"}), base_shift=Shift.FIRST)

    # Six consecutive weeks, crossing five Fridays.
    shifts = {effective_shift(op, start + datetime.timedelta(days=7 * i), cfg)
              for i in range(6)}
    assert shifts == {Shift.FIRST}, f"shift moved across a Friday: {shifts}"
```

Before writing this, confirm the real import path of `Shift` (it may be exported from `ppc_engine.domain.resources` rather than `ppc_engine.domain.shift`). Use whatever the codebase actually exposes; do not guess.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m pytest tests/test_operator_wiring.py -q -k never_changes`
Expected: FAIL, the set contains both `Shift.FIRST` and `Shift.SECOND`.

- [ ] **Step 3: Implement**

In `engine/new_engine.py`, replace the `week_anchor` line inside `_plan_config` (line 196):

```python
        # No shift rotation (2026-08-05): the shift an admin sets in Settings is the
        # shift the planner uses, every week. `week_anchor=None` is the engine's own
        # no-rotation path (ppc_engine/worktime.py: a None anchor returns base_shift
        # unchanged), so ppc_engine itself needs no change.
        week_anchor=None,
```

Leave `_friday_on_or_before` defined; it is still the app's `last_friday` idiom.

- [ ] **Step 4: Run it and watch it pass**

Run: `python3 -m pytest tests/test_operator_wiring.py -q -k never_changes`
Expected: PASS.

- [ ] **Step 5: Fix the two tests that asserted rotation**

`tests/test_plan_start_next_hour.py::test_plan_config_floor_moves_clock_not_rotation_anchor` asserts the anchor follows the plan date. That premise is gone. Rewrite it to keep the half that still matters (the clock floor) and assert the new rule:

```python
def test_plan_config_floor_moves_the_clock_and_there_is_no_rotation_anchor():
    # The floor rolls the CLOCK into the next day. There is no shift-rotation anchor
    # any more (2026-08-05): an admin's chosen shift holds every week.
    d = date(2025, 3, 6)
    rolled = _plan_config(Config(plan_start_date=d, plan_start_floor="2025-03-07T00:00"))
    assert rolled.plan_start == datetime(2025, 3, 7, 0, 0)
    assert rolled.week_anchor is None
```

Delete the now-unused `_friday_on_or_before` import in that test file if it becomes unused.

`tests/test_operator_wiring.py::test_prepare_contest_rotates_as_of_effective_start` exists only to prove the contest rotates. Read it first. If its ONLY purpose is rotation, delete it and say so in your report. If it also asserts something still true (e.g. that the contest uses the app's operator table at all), keep that part and drop the rotation assertion.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q`
Expected: only the 7 `test_rotate_*` failures in `tests/test_operator_master.py` remain (Task 2 handles them). Any other failure is a regression: stop and report.

- [ ] **Step 7: Commit**

```bash
git add engine/new_engine.py tests/test_operator_wiring.py tests/test_plan_start_next_hour.py
git commit -m "fix(engine): stop rotating operator shifts, the admin's setting holds"
```

---

### Task 2: The app's stored table stops rotating

**Files:**
- Modify: `engine/operator_master.py` (`rotate_table`, ~line 90-118)
- Test: `tests/test_operator_master.py`

**Interfaces:**
- Consumes: nothing from Task 1
- Produces: `rotate_table(table, today) -> (table, 0)` always. `operators_as_of(table, as_of)` returns the stored shifts for any date.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_operator_master.py`:

```python
def test_rotate_table_is_now_a_no_op_even_across_many_fridays():
    """Rotation was removed 2026-08-05. Whatever is stored is what is used."""
    table = {"week_anchor": "2026-07-03",
             "operators": [
                 {"id": "1", "name": "A", "machines_raw": "CNC1",
                  "shift": "First shift", "pinned": False},
                 {"id": "2", "name": "B", "machines_raw": "CNC2",
                  "shift": "Second shift", "pinned": False},
             ]}
    out, flips = rotate_table(table, date(2026, 8, 21))   # seven Fridays later
    assert flips == 0
    assert [r["shift"] for r in out["operators"]] == ["First shift", "Second shift"]


def test_operators_as_of_returns_the_stored_shift_for_any_date():
    table = {"week_anchor": "2026-07-03",
             "operators": [{"id": "1", "name": "A", "machines_raw": "CNC1",
                            "shift": "First shift", "pinned": False}]}
    for day in (date(2026, 7, 1), date(2026, 8, 21), date(2027, 1, 1)):
        assert operators_as_of(table, day)[0].shift == "First shift"
```

Match the file's existing import style for `rotate_table` / `operators_as_of` / `date`.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m pytest tests/test_operator_master.py -q -k "no_op_even_across or stored_shift_for_any_date"`
Expected: FAIL, shifts flipped and `flips` is 7.

- [ ] **Step 3: Implement**

Replace the body of `rotate_table` in `engine/operator_master.py`, keeping the function and its `(table, flips)` return shape:

```python
def rotate_table(table: dict, today: date):
    """No-op since 2026-08-05: operator shifts no longer rotate.

    The shift an admin sets in Settings is the shift the planner uses, every week,
    until an admin changes it. Kept (rather than deleted) because it is the shared
    expression every wiring site calls through -- `operators_as_of`, the display
    overlay, the contest setup -- and because its `(new_table, flips_applied)`
    contract is unpacked by those callers. Always returns the table untouched and
    zero flips, so the stored `week_anchor` and per-row `pinned` fields are inert.
    """
    return table, 0
```

Do NOT delete `_fridays_after`, `_is_two_shift`, `_flip_shift`, `last_friday` or `next_rotation` in this task; other code and tests still import them. Task 3 handles `next_rotation`'s UI use.

- [ ] **Step 4: Run it and watch it pass**

Run: `python3 -m pytest tests/test_operator_master.py -q -k "no_op_even_across or stored_shift_for_any_date"`
Expected: PASS.

- [ ] **Step 5: Remove the seven tests that existed only to prove rotation**

In `tests/test_operator_master.py`, delete these, which assert behaviour that has been deliberately removed:

- `test_rotate_flips_unpinned_two_shift_operators_across_one_friday`
- `test_rotate_flips_lowercase_shift_text_case_insensitively`
- `test_rotate_never_flips_a_pinned_operator`
- `test_rotate_never_flips_a_blank_shift_operator`
- `test_rotate_catch_up_two_fridays_nets_no_change_for_unpinned`
- `test_rotate_idempotent_same_day`
- `test_rotate_anchor_advances_to_last_counted_friday`

Read each one first. If any asserts something OTHER than rotation as well (for example that `to_operators` parses `machines_raw` correctly), keep that part as its own test rather than losing the coverage. Report anything you preserved.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green. Report the count.

- [ ] **Step 7: Commit**

```bash
git add engine/operator_master.py tests/test_operator_master.py
git commit -m "fix(operators): the stored shift table no longer rotates"
```

---

### Task 3: Analytics stops rotating, and the UI drops Stays and Next rotation

**Files:**
- Modify: `engine/analytics.py:~240` (the `rotate` flag)
- Modify: `web/index.html:207` (the `Stays` header cell)
- Modify: `web/app.js` (~1938-1952 the two operator row renders, ~1976-1982 the header line, ~33 and ~465 the status-strip segment)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `rotate_table` is a no-op (Task 2); `_plan_config(...).week_anchor is None` (Task 1)
- Produces: no new interfaces

- [ ] **Step 1: Write the failing test**

Add to `tests/test_analytics.py`, matching that file's existing helpers for building a schedule and masters:

```python
def test_operator_capacity_uses_the_stored_shift_across_a_friday():
    """Analytics keeps its OWN copy of the rotation rule. With rotation removed,
    a plan spanning a Friday must measure a person against the shift on file for
    every day, not a rotated one."""
```

Read `tests/test_analytics.py` and `engine/analytics.py::_operator_available_hours` first, then write the body against the real helpers. The assertion that matters: for a window spanning at least one Friday, a first-shift operator's available hours equal the first-shift hours for every working day in the window, with no second-shift days mixed in. If you cannot construct that cleanly with the file's existing fixtures, say so in your report rather than writing a test that asserts nothing.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m pytest tests/test_analytics.py -q -k stored_shift_across`
Expected: FAIL, capacity is computed against a rotated shift.

- [ ] **Step 3: Implement the analytics change**

In `engine/analytics.py`, the `rotate` flag (~line 240) currently reads:

```python
        rotate = getattr(config, "scheduler", "classic") == "new"
```

Replace it, keeping the variable so the `_operator_available_hours(...)` call below is untouched:

```python
        # Shift rotation was removed 2026-08-05: every operator works the shift on
        # file in Settings, every week. Capacity must match, so never rotate here.
        rotate = False
```

- [ ] **Step 4: Run it and watch it pass**

Run: `python3 -m pytest tests/test_analytics.py -q -k stored_shift_across`
Expected: PASS.

- [ ] **Step 5: Remove the Stays column from the HTML**

In `web/index.html`, delete the header cell on line 207:

```html
              <th>Stays</th>
```

- [ ] **Step 6: Remove the Stays cells from both row renders**

In `web/app.js` `renderOperatorsTable`, the read-only branch has:

```javascript
        <td>${o.pinned ? "Yes" : "-"}</td>
```

and the admin branch has:

```javascript
      <td><input type="checkbox" class="op-pinned" data-id="${id}" ${o.pinned ? "checked" : ""} /></td>
```

Delete both lines. Then find the empty-state row, which spans the columns with `colspan="${isAdmin ? 5 : 4}"`, and reduce it to `colspan="${isAdmin ? 4 : 3}"` so it still spans the whole table.

Then find and delete the block that wires the checkbox, which looks like:

```javascript
    tbody.querySelectorAll(".op-pinned").forEach((chk) => {
      chk.addEventListener("change", () => patchOperator(chk.dataset.id, { pinned: chk.checked }));
    });
```

- [ ] **Step 7: Remove the Next rotation line and status-strip segment**

In `web/app.js` `loadOperators`, replace the header block:

```javascript
    nextRotation = data.next_rotation || null;   // status-strip "Next rotation" segment
    const header = $("operators-header");
    if (header) {
      header.textContent = data.next_rotation
        ? `Shifts rotate every Friday (effective from first shift). Next rotation: ${isoToDdmmyyyy(data.next_rotation)}.`
        : "Shifts rotate every Friday (effective from first shift).";
    }
```

with:

```javascript
    const header = $("operators-header");
    if (header) {
      header.textContent = "The shift you set here is used every week, until you change it.";
    }
```

Then delete the status-strip segment (~line 465):

```javascript
  if (currentRole === "admin" && nextRotation) {
    segs.push(`<span class="ss-seg">Next rotation: ${escapeHtml(isoToDdmmyyyy(nextRotation))}</span>`);
  }
```

and the now-unused declaration near line 33 (`let nextRotation = null;`). Search `web/app.js` for `nextRotation` afterwards and confirm zero remaining uses.

- [ ] **Step 8: Fix the panel explainer, which is now false**

In `web/index.html`, the operators panel explainer currently reads:

```html
        <p class="explainer">Who runs which machine, and on which shift. Shifts swap every
          Friday. This list is filled in once from your uploaded Excel. After that, add or edit
          operators here.</p>
```

Replace with:

```html
        <p class="explainer">Who runs which machine, and on which shift. The shift you set here
          is used every week until you change it. This list is filled in once from your uploaded
          Excel. After that, add or edit operators here.</p>
```

- [ ] **Step 9: Check the JS parses and run the full suite**

Run: `node --check web/app.js`
Expected: no output.

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add engine/analytics.py web/index.html web/app.js tests/test_analytics.py
git commit -m "fix(analytics,web): no rotation in capacity, drop Stays and Next rotation"
```

---

### Task 4: Prove the old stored data still loads, and the banner fires

**Files:**
- Test: `tests/test_operators_api.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_operators_api.py`, reusing that file's existing helpers (`_api`, `_admin_client`, `_seed_book`; read the top of the file and use the real names):

```python
def test_a_store_with_the_old_pinned_and_anchor_fields_still_loads(monkeypatch):
    """Rotation was removed but the fields stay on disk. An existing store must not
    500, and PATCHing `pinned` must still be accepted so nothing breaks mid-deploy."""
    m = _api(); _seed_book()
    admin = _admin_client(m)

    ops = admin.get("/operators").json()["operators"]
    assert ops, "seeded table expected"
    op_id = ops[0]["id"]

    r = admin.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 200
    assert admin.get("/operators").status_code == 200


def test_changing_an_operators_shift_flags_the_applied_plan_stale(monkeypatch):
    """The owner's requirement: if a shift changes, the banner must say the applied
    optimization no longer matches, the same way a settings change does."""
    m = _api(); _seed_book()
    admin = _admin_client(m)

    ops = admin.get("/operators").json()["operators"]
    target = next(o for o in ops if o["shift"] in ("First shift", "Second shift"))

    # Pin an "applied optimization" whose inputs signature matches the book right now.
    sig = m._inputs_signature(m._resolve_config(m._load_plan_config()))
    m.book_store.save_plan_priority({}, {"saved_at": "2026-08-05T10:00:00",
                                         "inputs_sig": sig})
    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["inputs_changed"] is False

    flipped = "Second shift" if target["shift"] == "First shift" else "First shift"
    assert admin.patch(f"/operators/{target['id']}", json={"shift": flipped}).status_code == 200

    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["inputs_changed"] is True
```

Check `_inputs_signature`'s real signature before using it; if it takes no argument or a different one, adapt. The assertion that matters is the before/after of `inputs_changed`, not how the signature is obtained.

- [ ] **Step 2: Run them**

Run: `python3 -m pytest tests/test_operators_api.py -q -k "old_pinned or flags_the_applied_plan_stale"`
Expected: both PASS if Tasks 1-3 are correct and the fingerprint already covers shift, which the spec says it does. **If the banner test fails, do not change the test.** Report it: it means the fingerprint does not cover the shift and the spec's assumption was wrong, which is a finding the controller must adjudicate.

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_operators_api.py
git commit -m "test: old operator fields still load, and a shift change flags the plan stale"
```

---

### Task 5: Documentation

**Files:**
- Modify: `CLAUDE.md` (the operator-master rotation bullet, and the `web/` Settings bullet)

- [ ] **Step 1: Update `CLAUDE.md`**

Find the bullet beginning **"Operator & shift master rotation (2026-07-18…"** and the `web/` bullet describing the Settings "Operators & shifts" panel. Both currently describe the Friday rotation and the "Stays" pin as live behaviour. Rewrite them to say:

- Rotation was **removed 2026-08-05**; spec `docs/superpowers/specs/2026-08-05-manual-operator-shifts-design.md`.
- The shift an admin sets in Settings is the shift the planner uses, every week.
- Why it was removed: the pin never reached the planner. `engine/new_engine.py` builds the engine operator from four fields and `ppc_engine`'s `Operator` has no pin, so `ppc_engine/worktime.py` rotated everyone unconditionally. A director saw a pinned operator on 1st shift in week 1 and 2nd in week 2.
- `week_anchor=None` is now passed to the engine, which is its native no-rotation path; `ppc_engine` was not modified.
- `rotate_table` is a retained no-op returning `(table, 0)`; the `pinned` and `week_anchor` fields stay in the store and the API, inert, so no migration was needed.
- `engine/analytics.py` had a SECOND copy of the rotation rule for operator capacity; it no longer rotates either.

Keep the surrounding bullet structure and wrapping style.

- [ ] **Step 2: Run the full suite one last time**

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record that operator shifts are admin-owned, no rotation"
```

---

## Self-Review

**Spec coverage:**
- Engine stops rotating → Task 1
- App table stops rotating → Task 2
- Analytics stops rotating → Task 3 (steps 1-4)
- Stays column, checkbox and wiring removed → Task 3 (steps 5-6)
- Next rotation line and status-strip segment removed → Task 3 (step 7)
- Panel explainer corrected → Task 3 (step 8)
- Stored fields stay dormant, no migration → Task 4 (step 1, first test)
- Banner verified end to end rather than assumed → Task 4 (step 1, second test)
- Documentation → Task 5
- `ppc_engine` untouched → Global Constraints, and Task 1 implements via `week_anchor=None`

**Type consistency:** `rotate_table` returns `(table, 0)` in Task 2 and is unpacked by `operators_as_of` unchanged. `week_anchor is None` is asserted in Task 1 and relied on in Task 3. `nextRotation` is removed in one place (Task 3 step 7) and searched for afterwards.

**Known judgement calls left to the implementer, each flagged in place:** the real import path of `Shift` (Task 1 step 1); whether `test_prepare_contest_rotates_as_of_effective_start` asserts anything beyond rotation (Task 1 step 5); whether any of the seven deleted rotation tests also covered something still true (Task 2 step 5); and how to build the analytics fixture (Task 3 step 1). Each says to report rather than guess.
