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

This is the **complete** list of every variable the code reads — audited against
the source, not from memory. Nothing outside this list has any effect.

### Must be set on Render

| Variable | Meaning |
|---|---|
| `MONGODB_URI` | **This** deployment's own database (read/write). A separate cluster. |
| `UPSTREAM_MONGODB_URI` | The live site's database, **read-only user**. Setting this turns on the mirror. Unset ⇒ plain standalone copy. |
| `ADMIN_PASSWORD` | Admin login. **There is no baked default** — unset means a fresh random secret at every boot, i.e. no working admin login at all. Use a **different** password from the live site. |
| `USER_PASSWORD` | Floor login. Same rule: no default, must be set. |

### Set in `render.yaml`, no action needed

| Variable | Value | Why |
|---|---|---|
| `DEFAULT_SCHEDULER` | `new` | The engine the live site runs. |
| `GITHUB_REPO` | `riittiin/anvitech-ppc-duplicate` | Dispatch target. `api/main.py` refuses the mirrored repo regardless. |
| `ORACLE_CLAIM_TIMEOUT_MIN` | `0` | No Oracle box here; skip the 3-minute claim window. |
| `UPSTREAM_CACHE_TTL` | `30` | Seconds to cache live-site reads. |

### Cloud Optimize — see the section below

`GITHUB_DISPATCH_TOKEN`, `OPTIMIZE_WORKER_SECRET` on Render; `APP_URL` and
`OPTIMIZE_WORKER_SECRET` as GitHub repo secrets.

### Optional, with sane defaults — set only deliberately

| Variable | Default | Notes |
|---|---|---|
| `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` | `400` | Plans per contest candidate — **the deep-search depth knob**. Total plans shown in the UI = this × candidates × 2 (the machine-set dimension). At 12 overlap candidates that is 24 jobs: `400` ⇒ 9,600 plans, `700` ⇒ 16,800. Raising it raises wall clock proportionally; see the warning below. |
| `OPTIMIZE_CLOUD_TIMEOUT_MIN` | `40` | How long the app waits for the cloud contest before falling back to the local search on Render's 0.1 CPU. |
| `OPTIMIZE_WORKFLOW` | `optimize.yml` | Which workflow file to dispatch. |
| `ADMIN_USERNAME` | `anvitech` | Usernames are not secret. |
| `USER_USERNAME` | `anvitech_user` | |
| `APP_USERNAME` / `APP_PASSWORD` | — | **Legacy admin override that WINS over `ADMIN_USERNAME`/`ADMIN_PASSWORD`.** The live site has these set. If you copy the live site's env wholesale, these silently give this deployment the live site's admin credentials — set them to this deployment's own values or do not set them at all. |
| `SESSION_SECRET` | — | Leave unset. It falls back to a value persisted in this deployment's own store, and `anvitech:session_secret` is in `overlay_store.NEVER_MIRRORED`, so logins here can never be signed with the live site's secret. |
| `UPSTASH_REDIS_REST_URL` / `_TOKEN` | — | Alternative store, ranked below Mongo. Unused here. |
| `STORE_DIR` | `data/store` | Local-file store path; only used when no Mongo/Upstash is configured. |
| `AUTO_OPTIMIZE` | — | **Test isolation only. Never set this in a deployment** — `AUTO_OPTIMIZE=0` disables the auto re-optimize the Done button triggers. |
| `REGEN_GOLDEN` | — | Golden-trace regeneration, local dev only. |

### Sizing the deep search

`OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` is the one knob that can make Optimize
appear broken. The contest fans out over 20 GitHub Actions shards; with 24 jobs
six shards get two jobs each, so the slowest shard runs
`2 × budget × seconds-per-plan`. One plan on the owner's book is ~0.4 s on a
runner, so `400` ⇒ ~5 min and `700` ⇒ ~10 min of solving. Push it far enough and
the shard passes `OPTIMIZE_CLOUD_TIMEOUT_MIN`, at which point the app abandons
the cloud result and recomputes locally on 0.1 CPU — which looks like "the
optimizer got very slow" and is really "the cloud contest is being thrown away".

If the goal is for this deployment to produce the **same plans** as the live
site, this variable must hold the **same value** on both.

### A note on the plan count in the UI

If the "tried N of M plans" total is not a clean multiple of
`budget × 12 × 2`, the saved `overlap_percent` is off the contest grid.
`optimizer.sweep_contenders` prepends the current overlap as an extra contender
when it is not already one of `CLOUD_NEW_OVERLAP_CANDIDATES`
(60, 65, 70, 74, 78, 80, 82, 84, 86, 88, 90, 93) — 13 contenders instead of 12,
so the total grows by `budget × 2`. That is a stored setting, not a bug. It
happens after applying a result searched under a different engine's grid.

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

## Cloud Optimize (GitHub Actions)

"Start deep search" runs the full ~2,400-plan contest on a GitHub runner rather
than Render's 0.1 CPU. It needs config on **both** sides.

### On Render

| Variable | Value |
|---|---|
| `GITHUB_DISPATCH_TOKEN` | a GitHub PAT with **Actions: read and write** on this repo |
| `OPTIMIZE_WORKER_SECRET` | any long random string — **a new one**, not the live site's |
| `GITHUB_REPO` | `riittiin/anvitech-ppc-duplicate` |
| `ORACLE_CLAIM_TIMEOUT_MIN` | `0` |

### On GitHub (this repo → Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `APP_URL` | `https://anvitech-ppc-duplicate.onrender.com` |
| `OPTIMIZE_WORKER_SECRET` | **the same string** as the Render variable above |

With either Render variable missing, Optimize still works — it just computes
locally and slowly. Nothing breaks.

### Why the secret must be new

`OPTIMIZE_WORKER_SECRET` is what a worker presents to prove it may post contest
results. If the duplicate reused the live site's secret, a worker from either
deployment could post results into the other. Use a fresh value.

### The dispatch guard

`api/main.py` refuses to dispatch a contest into
`riittiin/anvitech-ppc-engine`, the repo this deployment mirrors — even if
`GITHUB_REPO` is explicitly set to it. That road leads to production just as
surely as a database write: it would consume the live site's Actions minutes,
and that repo's `APP_URL` secret points at the live app, so its workers would
post results into production. Covered by `tests/test_mirror_dispatch_guard.py`.

### The Oracle box

The live site has an always-on VM polling `/optimize/pending`
(`scripts/oracle_optimize_worker.py`) that claims contests before GitHub gets
them. This duplicate has no such box, so `ORACLE_CLAIM_TIMEOUT_MIN=0` skips the
claim window. Leave it at `0` unless you point a worker at this deployment —
and if you do, give it this deployment's `APP_URL` and secret, never the live
site's.

## Keep-warm

`.github/workflows/keep-warm.yml` pings this deployment every 12 minutes during
working hours so Render's free tier doesn't sleep it. It targets
`anvitech-ppc-duplicate.onrender.com`; the inherited version pointed at the live
site. Actions minutes are free on public repos.

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
