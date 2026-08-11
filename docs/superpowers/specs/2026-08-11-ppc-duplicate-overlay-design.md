# Anvitech PPC Duplicate — one-way mirror of the live app

**Date:** 2026-08-11
**Status:** approved, in implementation

## Problem

`anvitech-ppc-engine` runs live on Render against a MongoDB Atlas cluster. Someone
at Anvitech records production actuals in its **Daily Entry** tab every day.

We want a second, independent deployment — its own Render URL, its own MongoDB
cluster, its own public GitHub repo — that satisfies two clauses:

1. **Live data flows in.** Daily entries (and everything else) recorded on the
   original appear automatically in the duplicate.
2. **Nothing flows back.** No edit made in the duplicate can reach the original,
   by any path.

The duplicate is a place to experiment against real, current production data
without the risk of touching production.

## Non-goals

- Changing the original deployment in any way. It is read from and never written
  to, and its source is not modified.
- Two-way sync, conflict resolution UI, or merge history.
- Any change to the scheduling engines or the API surface.

## Architecture

```
ORIGINAL (untouched)
  anvitech-ppc-engine @ Render ──read/write──▶ Mongo A
                                                 │
DUPLICATE                                   read-only user
  anvitech-ppc-duplicate @ Render ──read────────▶│
        (new URL)                 ──read/write──▶ Mongo B (new cluster)
```

The duplicate process holds two store connections. Every write lands in Mongo B.
Mongo A is reached only through a read-only Atlas user.

### Why an overlay at the store layer

`engine/storage.py` already abstracts persistence behind a small interface that
the whole app funnels through:

```
kv_get / kv_set
hgetall / hset / hdel
list_all / list_append / list_set
delete_key
```

Every piece of state lives under a handful of keys (`engine/book_store.py`):

| Key | Type | Holds |
|---|---|---|
| `anvitech:actuals` | list | **Daily Entry data** |
| `anvitech:orders` | hash | active order book |
| `anvitech:orders:completed` | hash | archived orders |
| `anvitech:masters` | kv | routing/master workbook (base64) |
| `anvitech:plan_config`, `:operators`, `:absences`, … | kv | settings |

Implementing the mirror *at this interface* means the ~60 engine modules and the
3.2k-line `api/main.py` are untouched. One new file, one small edit to
`get_store()`.

## Component: `engine/overlay_store.py`

`OverlayStore(upstream, local)` implements the store interface with
copy-on-write semantics.

- `upstream` — Mongo A, wrapped in `ReadOnlyStore`, which raises
  `ReadOnlyStoreError` on every mutating method.
- `local` — Mongo B, read/write.

### Semantics by key type

**kv — whole-value copy-on-write.**
Read returns the local value if the key has ever been written locally, otherwise
the upstream value. Any write makes the key locally owned from then on.

**hash — per-field merge.** This is the important one for the order book.
Read returns upstream fields overlaid with local fields (local wins), minus
fields tombstoned locally. So an order added on the real site still appears in
the duplicate, while an order the duplicate edited stays local. `hset` writes
locally; `hdel` writes a tombstone rather than attempting an upstream delete.

**list — per-entry merge.** This is what makes clause 1 hold for Daily Entry.
Read returns upstream entries (minus tombstoned ids) followed by local entries.
`list_append` appends locally. `list_set(values)` — used by `delete_actual` and
`delete_orders`, which rewrite the whole list — is **diffed** against the current
effective list:

- entry present before, absent now → tombstone its id
- entry absent before, present now → local append
- entry whose content changed → tombstone upstream id + local append

The naive alternative ("any local write detaches the key") would silently break
clause 1 the first time a user deleted an entry in the duplicate: the key would
detach and new upstream daily entries would stop arriving. Diffing by entry id
means entries that appear upstream *later* still flow through, because they were
never tombstoned.

Entry identity is `json["id"]` when present, else a SHA-256 of the canonical
JSON. Generic rather than actuals-specific.

**`delete_key` — deliberate detach.** The admin "wipe everything" button sets a
whole-key detach marker: the key becomes fully local and upstream is ignored for
it. A deliberate wipe should not have upstream data reappear underneath it. A
**"Re-attach to live"** control in Settings clears detach markers and tombstones
for a key.

### Local bookkeeping

The overlay stores its own metadata in Mongo B under a reserved prefix:

- `overlay:owned:<key>` — set of locally-owned kv keys / hash fields
- `overlay:tomb:<key>` — set of tombstoned hash fields / list entry ids
- `overlay:detached:<key>` — whole-key detach marker

Reserved-prefix keys are never mirrored and never visible to the app.

## Clause 2 enforcement

Three independent layers, any one of which is sufficient:

1. **Atlas read-only database user** on Mongo A. The real guarantee — enforced by
   the database, outside our process.
2. **`ReadOnlyStore` proxy** raises on any mutating call before it reaches the
   driver.
3. **Separate env var.** `UPSTREAM_MONGODB_URI` is only ever handed to the
   upstream connection; `MONGODB_URI` (Mongo B) is the only one the write path
   sees.

## Configuration

| Env var | Meaning |
|---|---|
| `MONGODB_URI` | Mongo B — the duplicate's own read/write store |
| `UPSTREAM_MONGODB_URI` | Mongo A, read-only user. **Unset ⇒ overlay disabled** |
| `UPSTREAM_CACHE_TTL` | seconds to cache upstream reads (default 30) |
| `ADMIN_PASSWORD` / `USER_PASSWORD` | fresh credentials, distinct from the original |

When `UPSTREAM_MONGODB_URI` is unset the overlay never engages and the app
behaves identically to the original. This keeps the existing test suite green
and makes local development trivial.

## Performance

The per-request read cache already in `storage.py` sits *above* the overlay, so
each key is fetched at most once per request. On top of that, upstream reads get
a short TTL cache (default 30s), since the duplicate now talks to two clusters
per request and Render's free plan is latency-sensitive.

## Clause 1, end to end

`api/main.py` hashes the full actuals content into the plan-cache signature
(`_current_book_sig` / the `"actuals"` digest). When someone saves a daily entry
on the original, the upstream list changes → the digest changes → the duplicate
invalidates its cached plan and re-plans against the new actuals. The mirror
drives the schedule, not just the table view. **To be verified by test, not
assumed.**

## Testing

1. `OverlayStore` unit tests against two `LocalStore` instances — one standing in
   for upstream, one for local. Covers every row of the semantics table.
2. A clause-2 test asserting `ReadOnlyStore` raises on every mutating method and
   that no overlay operation ever calls a write on upstream (spy backend).
3. A clause-1 integration test: append an actual to the upstream store, assert it
   appears in `book_store.load_actuals()` and that the plan-cache signature moves.
4. The existing 118-file suite must stay green with the overlay disabled.

## Deployment

1. Fresh `git init` — no history from the original repo carried over.
2. New public GitHub repo.
3. New MongoDB Atlas cluster (Mongo B) + read-only user on Mongo A.
4. New Render web service from `render.yaml`, service renamed.
5. Fresh admin/user passwords.

## Deviations from "exact replica"

- A **"DUPLICATE"** badge in the header. Two identical-looking tabs where one is
  production is a foot-gun. Cosmetic and trivially removable.
- `delay-justification-2026-08-09 (1).xlsx` is not carried over — it is a real
  production data artifact and an output, not source.

## Note on the original repo

`delay-justification-2026-08-09 (1).xlsx` is **tracked in the original public
repo**. It contains real production data. Unrelated to this work, but worth
addressing separately.
