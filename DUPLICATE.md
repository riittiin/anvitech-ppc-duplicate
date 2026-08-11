# Anvitech PPC — Duplicate

An independent copy of the live Anvitech PPC app: its own Render URL, its own
MongoDB cluster, its own GitHub repo. It mirrors production's data one way.

**Clause 1** — daily entries and orders recorded on the live site appear here
automatically.
**Clause 2** — nothing done here can reach the live site, ever.

Everything else is a full copy of the app: same engines, same UI, same tests.

## How the mirror works

`engine/overlay_store.py` layers this deployment's writes over a read-only view
of production's database, at the storage interface the whole app already uses.
No engine module or API route needed changing.

| Data | Behaviour |
|---|---|
| Daily entries | Production's entries appear here. Entries you add stay local. Deleting one here does **not** stop new ones arriving. |
| Order book | Merged per order. An order added on the live site shows up here; an order you edit here stays yours. |
| Masters, settings, operators | Read from production until you change them here, then yours forever. |
| Session secret | Never mirrored — logins are completely separate from the live site. |

Wiping data here (admin → "delete everything") **detaches** that key: production's
rows won't quietly reappear underneath the wipe. Settings → **Live site mirror**
lists anything detached and offers "Re-attach to live" to undo it.

The header shows an orange **DUPLICATE** badge whenever mirroring is on. Do not
remove it — two identical-looking tabs, one of them production, is expensive to
confuse.

## Environment variables

| Variable | Meaning |
|---|---|
| `MONGODB_URI` | **This** deployment's own database (read/write). A separate cluster. |
| `UPSTREAM_MONGODB_URI` | The live site's database, **read-only user**. Setting this turns on the mirror. Unset ⇒ plain standalone copy. |
| `UPSTREAM_CACHE_TTL` | Seconds to cache live-site reads. Default 30. |
| `ADMIN_PASSWORD` / `USER_PASSWORD` | Logins for this deployment. Use **different** passwords from the live site. |
| `DEFAULT_SCHEDULER` | `new` (set in `render.yaml`). |

Usernames default to `anvitech` (admin) and `anvitech_user` (user); override with
`ADMIN_USERNAME` / `USER_USERNAME`.

## Setting it up

### 1. MongoDB

**This deployment's own cluster** — a new free M0 cluster in Atlas, a normal
read/write user. Its connection string becomes `MONGODB_URI`.

**Read-only access to the live cluster** — on the cluster the live site already
uses, add a *new* database user:

- Atlas → Database Access → Add New Database User
- Built-in role: **Only read any database**
- Never reuse the live site's existing read/write user here

That user's connection string becomes `UPSTREAM_MONGODB_URI`. This is the real
guarantee behind clause 2 — enforced by the database, not by application code.
`ReadOnlyStore` is a second line of defence, not the first.

### 2. Render

New Web Service from this repo (`render.yaml` covers build and start). Set the
secret env vars in the dashboard — they are all `sync: false`, so none of them
live in the repo.

### 3. GitHub

The repo is public, like the original. No password or connection string is baked
into the source; `.gitignore` already excludes every real-data workbook and
export. Keep it that way.

## Running locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt httpx
.venv/bin/python -m pytest -q
.venv/bin/uvicorn api.main:app --reload
```

With no `UPSTREAM_MONGODB_URI` set, the overlay never engages and the app behaves
exactly like the original — which is why the whole inherited test suite still
passes unchanged.

Python 3.12 is what the tests are verified against. 3.14 currently fails at
import on an openpyxl/numpy incompatibility, unrelated to this app.

## Tests

- `tests/test_overlay_store.py` — merge semantics, and that no overlay operation
  ever writes upstream
- `tests/test_overlay_wiring.py` — `get_store()` wiring, plus clause 1 end to end
  through `book_store` and the plan cache
- `tests/test_mirror_api.py` — `/mirror/status` and `/mirror/reattach`

Everything else is inherited from the original app.
