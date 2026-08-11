"""Per-request store read cache (engine.storage._RequestCachedStore + the API
middleware). It removes redundant durable-store round-trips within a single HTTP
request WITHOUT changing the data returned: reads are memoized per key, a write to
a key drops its cached reads (read-after-write stays correct), and the cache is
active only inside a request (tests/engine/background threads use the raw backend)."""
import os
import tempfile

import pytest

from engine import storage


class _CountingBackend:
    """A minimal store backend that counts read calls per key."""
    def __init__(self):
        self.reads = []                      # (op, key) for each backend read
        self.kv = {}
        self.h = {}
        self.l = {}

    def kv_get(self, key):
        self.reads.append(("kv", key)); return self.kv.get(key)
    def kv_set(self, key, value):
        self.kv[key] = value
    def hgetall(self, key):
        self.reads.append(("h", key)); return dict(self.h.get(key, {}))
    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value
    def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)
    def list_all(self, key):
        self.reads.append(("l", key)); return list(self.l.get(key, []))
    def list_append(self, key, value):
        self.l.setdefault(key, []).append(value)
    def list_set(self, key, values):
        self.l[key] = list(values)
    def delete_key(self, key):
        self.kv.pop(key, None); self.h.pop(key, None); self.l.pop(key, None)


def test_reads_memoized_once_per_key():
    b = _CountingBackend(); b.kv["k"] = "v0"
    s = storage._RequestCachedStore(b, {})
    assert s.kv_get("k") == "v0"
    assert s.kv_get("k") == "v0"
    assert s.kv_get("k") == "v0"
    assert b.reads == [("kv", "k")]          # one backend read for three calls


def test_write_invalidates_so_read_after_write_is_fresh():
    b = _CountingBackend(); b.kv["k"] = "v0"
    s = storage._RequestCachedStore(b, {})
    assert s.kv_get("k") == "v0"
    s.kv_set("k", "v1")                       # write drops the cached read
    assert s.kv_get("k") == "v1"             # fresh, not the stale "v0"
    assert b.reads == [("kv", "k"), ("kv", "k")]


def test_hash_and_list_memoized_and_invalidated():
    b = _CountingBackend()
    b.h["orders"] = {"a": "1"}; b.l["acts"] = ["x"]
    s = storage._RequestCachedStore(b, {})
    assert s.hgetall("orders") == {"a": "1"}
    assert s.hgetall("orders") == {"a": "1"}
    assert s.list_all("acts") == ["x"]
    assert s.list_all("acts") == ["x"]
    assert b.reads == [("h", "orders"), ("l", "acts")]   # one each
    s.hset("orders", "b", "2")               # invalidates the hash key
    assert s.hgetall("orders") == {"a": "1", "b": "2"}
    s.list_append("acts", "y")               # invalidates the list key
    assert s.list_all("acts") == ["x", "y"]
    assert b.reads.count(("h", "orders")) == 2
    assert b.reads.count(("l", "acts")) == 2


def test_returned_dict_and_list_are_copies_not_the_cache():
    """A caller mutating a returned hash/list must NOT corrupt the cached value —
    the next read returns the true stored value."""
    b = _CountingBackend(); b.h["orders"] = {"a": "1"}; b.l["acts"] = ["x"]
    s = storage._RequestCachedStore(b, {})
    got = s.hgetall("orders"); got["a"] = "TAMPERED"; got["z"] = "9"
    assert s.hgetall("orders") == {"a": "1"}          # cache intact
    gotl = s.list_all("acts"); gotl.append("TAMPERED")
    assert s.list_all("acts") == ["x"]                 # cache intact


def test_get_store_wraps_only_within_a_request_scope(monkeypatch):
    monkeypatch.setenv("STORE_DIR", tempfile.mkdtemp())
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    # Outside a request: the raw backend, never the wrapper.
    assert not isinstance(storage.get_store(), storage._RequestCachedStore)
    token = storage.begin_request_cache()
    try:
        inside = storage.get_store()
        assert isinstance(inside, storage._RequestCachedStore)
    finally:
        storage.end_request_cache(token)
    assert not isinstance(storage.get_store(), storage._RequestCachedStore)


def test_endpoint_reads_each_key_at_most_once(monkeypatch):
    """End-to-end: within a single /run request the plan config and absences keys
    are each read exactly once — before this cache they were read twice. Proves the
    middleware installs the request cache and it survives into the (threadpool) endpoint."""
    pytest.importorskip("fastapi")
    monkeypatch.setenv("STORE_DIR", tempfile.mkdtemp())
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    import importlib
    import api.main as m
    importlib.reload(m)
    from fastapi.testclient import TestClient
    from engine import book_store, operator_master
    from engine.models import Order
    from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B
    from datetime import date

    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
                           Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21))])
    # Pre-seed the operator table so no one-time seeding WRITE happens mid-request
    # (a write would legitimately invalidate the cache and skew the count).
    m._current_masters()

    store = book_store.get_store()            # raw backend (no request active here)
    reads = []
    for meth in ("kv_get", "hgetall", "list_all"):
        orig = getattr(store, meth)
        def mk(orig, meth):
            def wrapped(key, *a, **k):
                reads.append((meth, key)); return orig(key, *a, **k)
            return wrapped
        setattr(store, meth, mk(orig, meth))

    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    reads.clear()
    r = c.post("/run", json={})
    assert r.status_code == 200
    assert reads.count(("kv_get", book_store.PLAN_CONFIG_KEY)) == 1
    assert reads.count(("kv_get", book_store.ABSENCES_KEY)) == 1
    assert reads.count(("hgetall", book_store.ORDERS_KEY)) == 1
