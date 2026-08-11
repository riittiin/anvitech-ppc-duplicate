# Operator machine picker — no more free-typed machine lists (2026-08-04)

## Problem

Settings → **Operators & shifts** lets an admin type an operator's machines as free
text (`web/index.html:224`, `web/app.js:1816` — a bare `<input type="text">` PATCHed
verbatim into `machines_raw`). One wrong character silently destroys that operator's
qualifications, with no error anywhere in the app.

The failure is real, not theoretical, because the string is split by **two parsers that
disagree**:

| Parser | Used by | Splits on | Canonicalization |
|---|---|---|---|
| `ppc_engine/loaders/normalize.py` `parse_machine_options` | **the live engine** (`new_engine._apply_app_operators`) | `/` and `,` only | strip whitespace, uppercase — **keeps `.`, `&`, `-`** |
| `engine/loaders.py` `parse_resource_candidates` | old engine, `operator_coverage`, analytics | `/`, `,`, `&`, ` or ` | strip **all non-alphanumeric**, uppercase |

Consequences on live data today:

- `CNC1.CNC2` → the live engine sees ONE machine id `CNC1.CNC2`, which matches nothing.
  That operator is qualified for **zero** machines and simply stops being scheduled.
  Nothing warns anyone.
- `CNC1 & CNC2` → analytics shows two machines; the live scheduler sees one junk id.
  The two surfaces disagree about who can run what.

## Goal

Make it impossible to hand-type a machine list, and make any already-broken value
visible instead of silent.

Out of scope: the engine, scheduler, optimizer, storage schema, and the divergent
parsers themselves. `machines_raw` stays a string in the same store key; nothing about
how a plan is computed changes.

## Design

### 1. Serve the machine list (backend, read-only, ~4 lines)

`GET /operators` gains a `machines` array beside the existing `operators` /
`next_rotation`:

```json
{"id": "CNC1", "name": "CNC 1", "type": "CNC lathe", "provisional": false}
```

Source: `_current_masters().machines` — i.e. the uploaded Excel's **Machine master**
sheet, which is already parsed and cached (the endpoint calls `_current_masters()`
today, so this costs nothing extra). Sorted by `(provisional, type, id)`.

- Machines a routing references that are **not yet in Machine master** (the
  `provisional` ones, e.g. `CNC7`) **are included**, flagged `provisional: true`.
  Excluding them would make it impossible to qualify anyone to run them, so their
  work could never be staffed — the opposite of this project's "the master gets
  filled in later, never fail" rule.
- `OS` is never a machine and never appears (the loader already refuses to register it).
- No workbook uploaded yet → `machines: []`; the picker says so instead of offering
  an empty dropdown.

No other endpoint changes. `POST`/`PATCH` keep accepting `machines_raw` as a plain
string, so nothing else in the app or its tests moves.

### 2. The picker (frontend)

The Machines cell becomes **chips + one dropdown**, with no text input anywhere:

```
Machines: [CNC 1 ✕] [CNC 2 ✕] [VMC 1 ✕]   ( ＋ Add machine ▾ )
```

- **Chips** — one per selected machine, showing the master's display name, each with
  an ✕ that removes it.
- **＋ Add machine** — a single `<select>` listing only machines **not already
  selected**, grouped with `<optgroup>` by the master's machine type (CNC lathes,
  VMC, manual stations, inspection, and a trailing "Not in Machine master yet" group
  for provisional ones). Choosing an entry adds the chip and resets the select to its
  placeholder.
- The same control appears on the **Add operator** row, holding its selection locally
  until the row is submitted.
- **User (read-only) role** — the same chips as plain text: no ✕, no dropdown.

### 3. What gets written

Selected ids joined with `/` — e.g. `CNC1/CNC2/VMC1`.

`/` is the one separator **both** parsers agree on, and canonical ids contain no
spaces, dots or ampersands, so the value cannot be mis-split by either parser. Every
value this UI can produce is therefore unambiguous by construction.

### 4. Existing values are mirrored honestly, never silently "fixed"

To decide which chips to draw, the frontend parses `machines_raw` **exactly the way
the live engine does** — split on `/` and `,` only, then uppercase and strip
whitespace:

- token matches a known machine id → normal chip;
- token matches nothing → a red **unknown** chip showing the raw token, with an ✕.

This is deliberate. If the UI split on `&` too, `CNC1 & CNC2` would draw two healthy
chips while the scheduler sees one broken id — the panel would lie. Mirroring the live
parser means the panel shows exactly what the scheduler believes, so every currently
broken row (the `CNC1.CNC2` class of bug) becomes visible the moment the page loads,
and one click on ✕ removes it.

Nothing is auto-corrected and nothing is auto-deleted: an unknown token stays in
`machines_raw` until a human removes it.

An operator with no machines at all shows a plain note that they will not be
scheduled.

### 5. Saving and re-planning

Adding or removing a chip PATCHes immediately (same as the current input's `change`
handler — an edit is never lost), and the table re-renders from local state rather
than a full reload, so the dropdown doesn't flicker or lose position.

The follow-up `runPlan(false)` is **debounced ~1.2 s**. Today each edit triggers a
full re-plan; with chips, adding five machines would trigger five. Debouncing collapses
a burst of clicks into a single re-plan. Shift, pin and remove keep their current
immediate behaviour.

## Testing

- **API** (`tests/test_operators_api.py`): `GET /operators` returns `machines` with
  the sample workbook's ids/display names; provisional machines present and flagged;
  `OS` absent; `[]` before any upload; existing response fields unchanged.
- **Frontend**: no JS test harness exists in this repo, so the picker is verified in a
  real browser against the live-format workbook — add a machine, remove one, add an
  operator with two machines, confirm the stored `machines_raw` is `/`-joined ids,
  confirm a hand-planted `CNC1.CNC2` row renders as an unknown chip, and confirm the
  user role sees plain text with no controls.
- **Regression**: the full `pytest` suite must stay green (the engine is untouched, so
  the golden trace must not move).
