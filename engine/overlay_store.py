"""A one-way mirror of the live deployment, implemented at the store layer.

This duplicate reads production's data but can never write to it. Both halves
of that are implemented here:

* :class:`ReadOnlyStore` wraps the upstream backend and raises on every
  mutating method, so no code path can write to production even by mistake.
* :class:`OverlayStore` presents the *same* interface as the plain backends
  (kv / hash / list / delete_key), layering the duplicate's own writes on top
  of a read-only view of production. Because it is interface-compatible, no
  engine module and no API route needed to change — ``get_store()`` simply
  hands back an overlay when ``UPSTREAM_MONGODB_URI`` is configured.

Copy-on-write semantics, by key type:

**kv** — whole-value. A key reads through to upstream until the duplicate
writes it; from then on the local value wins forever.

**hash** (the order book) — per *field*. Upstream fields are merged with local
fields, local winning, minus fields deleted locally. An order added on the real
site therefore still shows up here, while an order edited here stays local.

**list** (the Daily Entry actuals) — per *entry*. Upstream entries appear,
minus ones deleted locally; local entries follow. ``list_set``, which the app
uses to rewrite the whole list on a delete, is *diffed* against the current
effective list rather than taken literally.

That last point is the reason this module exists rather than a simple
"any local write detaches the key" rule. Under that simpler rule, the first
time someone deleted one daily entry in the duplicate the whole key would go
local and new entries from production would silently stop arriving. Diffing by
entry identity means only the entries actually removed are tombstoned, so
entries that appear upstream later still flow through.

Bookkeeping (tombstones, detach markers) lives in the *local* store under the
reserved ``overlay:`` prefix and is never visible to the app.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

# Keys that must never read from upstream, no matter what.
#
# The session secret signs login cookies. If the duplicate adopted production's
# secret, a session cookie minted by the real site would validate here (and
# vice versa) — the two deployments must have completely separate logins.
NEVER_MIRRORED = frozenset({
    "anvitech:session_secret",
})

# Prefix for the overlay's own bookkeeping. App keys all start with "anvitech:",
# so there is no collision; the guard below makes that structural rather than
# a convention.
RESERVED_PREFIX = "overlay:"

_META_KEY = RESERVED_PREFIX + "meta:"
_DETACHED_INDEX = RESERVED_PREFIX + "detached_index"

_EMPTY_META = {"h_tomb": [], "l_tomb": [], "detached": False}


class ReadOnlyStoreError(RuntimeError):
    """Raised when something tries to write to the upstream (production) store."""


class ReadOnlyStore:
    """Read-only proxy over a backend store.

    The database-level guarantee is the read-only Atlas user on the upstream
    cluster; this class is the in-process second line of defence, so a write
    fails loudly here instead of being rejected (or worse, accepted) remotely.
    """

    def __init__(self, backend):
        self._b = backend

    # --- reads pass through --- #
    def kv_get(self, key) -> Optional[str]:
        return self._b.kv_get(key)

    def hgetall(self, key) -> dict:
        return self._b.hgetall(key)

    def list_all(self, key) -> list:
        return self._b.list_all(key)

    # --- writes are refused --- #
    def _refuse(self, op: str):
        raise ReadOnlyStoreError(
            f"refusing to {op} on the upstream store: this deployment is a "
            f"read-only mirror of production"
        )

    def kv_set(self, key, value: str) -> None:
        self._refuse("kv_set")

    def hset(self, key, field, value: str) -> None:
        self._refuse("hset")

    def hdel(self, key, field) -> None:
        self._refuse("hdel")

    def list_append(self, key, value: str) -> None:
        self._refuse("list_append")

    def list_set(self, key, values) -> None:
        self._refuse("list_set")

    def delete_key(self, key) -> None:
        self._refuse("delete_key")


class TTLCachedReads:
    """Short-lived read cache over the upstream store.

    The duplicate makes a second, cross-cluster round trip for every mirrored
    key. The per-request cache in ``storage.py`` already collapses repeats
    *within* one request; this collapses them across requests too. The TTL is
    the bound on how stale the mirror can be, so it is deliberately short.
    """

    def __init__(self, backend, ttl: float = 30.0, clock=None):
        import time
        self._b = backend
        self._ttl = float(ttl)
        self._clock = clock or time.monotonic
        self._cache: dict = {}

    def _get(self, op: str, key, fetch):
        if self._ttl <= 0:
            return fetch()
        now = self._clock()
        hit = self._cache.get((op, key))
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        value = fetch()
        self._cache[(op, key)] = (now, value)
        return value

    def kv_get(self, key) -> Optional[str]:
        return self._get("kv", key, lambda: self._b.kv_get(key))

    def hgetall(self, key) -> dict:
        return dict(self._get("h", key, lambda: self._b.hgetall(key)))

    def list_all(self, key) -> list:
        return list(self._get("l", key, lambda: self._b.list_all(key)))

    # Writes are the wrapped store's business — it refuses them.
    def kv_set(self, key, value: str) -> None:
        self._b.kv_set(key, value)

    def hset(self, key, field, value: str) -> None:
        self._b.hset(key, field, value)

    def hdel(self, key, field) -> None:
        self._b.hdel(key, field)

    def list_append(self, key, value: str) -> None:
        self._b.list_append(key, value)

    def list_set(self, key, values) -> None:
        self._b.list_set(key, values)

    def delete_key(self, key) -> None:
        self._b.delete_key(key)


class OverlayStore:
    """Local writes layered over a read-only upstream. See the module docstring."""

    def __init__(self, upstream, local):
        self._up = upstream
        self._local = local

    # ----------------------------------------------------------------- #
    # Bookkeeping
    # ----------------------------------------------------------------- #
    @staticmethod
    def _guard(key) -> None:
        if str(key).startswith(RESERVED_PREFIX):
            raise ValueError(
                f"{key!r} is overlay bookkeeping, not an application key"
            )

    def _meta(self, key) -> dict:
        raw = self._local.kv_get(_META_KEY + str(key))
        if not raw:
            return dict(_EMPTY_META)
        meta = dict(_EMPTY_META)
        meta.update(json.loads(raw))
        return meta

    def _write_meta(self, key, meta: dict) -> None:
        self._local.kv_set(_META_KEY + str(key), json.dumps(meta))

    def _local_only(self, key) -> bool:
        """True when upstream must be ignored for this key entirely."""
        return key in NEVER_MIRRORED or self._meta(key)["detached"]

    @staticmethod
    def _eid(raw) -> str:
        """Identity of a list entry.

        Actuals carry a stable ``id``; anything else falls back to a content
        hash, which is enough to recognise an unchanged entry.
        """
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("id") is not None:
            return "id:" + str(parsed["id"])
        return "sha:" + hashlib.sha256(str(raw).encode("utf-8")).hexdigest()

    # ----------------------------------------------------------------- #
    # kv — whole-value copy-on-write
    # ----------------------------------------------------------------- #
    def kv_get(self, key) -> Optional[str]:
        self._guard(key)
        local = self._local.kv_get(key)
        # `is not None`, not truthiness: "" is a real stored value that must
        # shadow upstream.
        if local is not None or self._local_only(key):
            return local
        return self._up.kv_get(key)

    def kv_set(self, key, value: str) -> None:
        self._guard(key)
        self._local.kv_set(key, value)

    # ----------------------------------------------------------------- #
    # hash — per-field merge
    # ----------------------------------------------------------------- #
    def hgetall(self, key) -> dict:
        self._guard(key)
        if self._local_only(key):
            return self._local.hgetall(key)
        tomb = set(self._meta(key)["h_tomb"])
        merged = {f: v for f, v in self._up.hgetall(key).items() if f not in tomb}
        merged.update(self._local.hgetall(key))
        return merged

    def hset(self, key, field, value: str) -> None:
        self._guard(key)
        self._local.hset(key, field, value)
        if not self._local_only(key):
            meta = self._meta(key)
            if field in meta["h_tomb"]:
                # Re-adding a field that was deleted here earlier.
                meta["h_tomb"] = [f for f in meta["h_tomb"] if f != field]
                self._write_meta(key, meta)

    def hdel(self, key, field) -> None:
        self._guard(key)
        self._local.hdel(key, field)
        if self._local_only(key):
            return
        meta = self._meta(key)
        if field not in meta["h_tomb"]:
            meta["h_tomb"] = sorted(set(meta["h_tomb"]) | {field})
            self._write_meta(key, meta)

    # ----------------------------------------------------------------- #
    # list — per-entry merge
    # ----------------------------------------------------------------- #
    def list_all(self, key) -> list:
        self._guard(key)
        if self._local_only(key):
            return self._local.list_all(key)
        tomb = set(self._meta(key)["l_tomb"])
        upstream = [v for v in self._up.list_all(key) if self._eid(v) not in tomb]
        return upstream + self._local.list_all(key)

    def list_append(self, key, value: str) -> None:
        self._guard(key)
        if not self._local_only(key):
            meta = self._meta(key)
            eid = self._eid(value)
            if eid in meta["l_tomb"]:
                upstream_copy = next(
                    (v for v in self._up.list_all(key) if self._eid(v) == eid), None)
                if upstream_copy == value:
                    # Re-adding exactly the entry that was deleted here: lift the
                    # tombstone and let upstream supply it again. Appending a local
                    # copy as well would show it twice.
                    meta["l_tomb"] = [t for t in meta["l_tomb"] if t != eid]
                    self._write_meta(key, meta)
                    return
                # Same id, different content — keep the upstream copy hidden and
                # store this version locally.
        self._local.list_append(key, value)

    def list_set(self, key, values) -> None:
        """Rewrite the effective list.

        The app calls this to delete an entry (it loads everything, drops one,
        and writes the rest back). Taken literally that would flatten the
        upstream half of the list into local storage and end the sync, so it is
        diffed instead: entries that disappeared are tombstoned, entries that
        upstream still supplies unchanged are left to upstream, and everything
        else is stored locally.
        """
        self._guard(key)
        values = list(values)
        if self._local_only(key):
            self._local.list_set(key, values)
            return

        meta = self._meta(key)
        tomb = set(meta["l_tomb"])
        upstream_visible = {
            self._eid(v): v
            for v in self._up.list_all(key)
            if self._eid(v) not in tomb
        }
        wanted = {self._eid(v) for v in values}

        # Gone from the effective list → hide the upstream entry.
        tomb |= {eid for eid in upstream_visible if eid not in wanted}

        local_values = []
        for value in values:
            eid = self._eid(value)
            upstream_value = upstream_visible.get(eid)
            if upstream_value is not None:
                if upstream_value == value:
                    continue          # unchanged; leave it to upstream
                tomb.add(eid)         # edited here; shadow the upstream copy
            local_values.append(value)

        self._local.list_set(key, local_values)
        meta["l_tomb"] = sorted(tomb)
        self._write_meta(key, meta)

    # ----------------------------------------------------------------- #
    # Detach / re-attach
    # ----------------------------------------------------------------- #
    def delete_key(self, key) -> None:
        """Wipe the key here and stop mirroring it.

        An explicit wipe (the admin "delete everything" button) should not have
        production's rows quietly reappear underneath it, so the key detaches.
        :meth:`reattach` undoes this.
        """
        self._guard(key)
        self._local.delete_key(key)
        self._write_meta(key, {"h_tomb": [], "l_tomb": [], "detached": True})
        self._set_detached_index(self.detached_keys() | {key})

    def reattach(self, key) -> None:
        """Discard everything this deployment did to ``key`` and mirror
        production again."""
        self._guard(key)
        self._local.delete_key(key)
        self._local.kv_set(_META_KEY + str(key), json.dumps(dict(_EMPTY_META)))
        self._set_detached_index(self.detached_keys() - {key})

    def detached_keys(self) -> set:
        raw = self._local.kv_get(_DETACHED_INDEX)
        return set(json.loads(raw)) if raw else set()

    def _set_detached_index(self, keys) -> None:
        self._local.kv_set(_DETACHED_INDEX, json.dumps(sorted(keys)))
