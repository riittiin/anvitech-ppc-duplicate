"""The overlay as the app actually sees it: through get_store() and book_store.

test_overlay_store.py covers the merge semantics in isolation. This module
checks the two things that only show up once the overlay is wired in:

* ``get_store()`` engages the overlay when UPSTREAM_MONGODB_URI is set, and
  stays completely out of the way when it isn't.
* Clause 1 end to end — a daily entry saved on the real site arrives through
  ``book_store.load_actuals()`` and moves the plan-cache signature, so the
  duplicate re-plans instead of serving a stale schedule.

Upstream rows are built from the real ``Actual`` / ``Order`` models and
serialised with ``to_json()``, so these tests track the models rather than a
hand-written copy of their shape.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from engine import book_store, storage
from engine.models import Actual, Order
from engine.overlay_store import OverlayStore, ReadOnlyStore, TTLCachedReads
from engine.storage import LocalStore


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """Point get_store() at an overlay whose upstream is a temp LocalStore,
    standing in for the production Mongo cluster."""
    up = LocalStore(tmp_path / "upstream")
    local = LocalStore(tmp_path / "local")
    overlay = OverlayStore(ReadOnlyStore(up), local)
    monkeypatch.setattr(storage, "get_store", lambda: overlay)
    monkeypatch.setattr(book_store, "get_store", lambda: overlay)
    return up


def an_actual(aid: str, day: str = "2026-08-11", produced: float = 40) -> Actual:
    return Actual(so_no="SO1", item_code="ITEM-A", entry_date=date.fromisoformat(day),
                  qty_produced=produced, id=aid, process="TURNING", operator="OP1")


def an_order(so_no: str = "SO1", item_code: str = "ITEM-A") -> Order:
    return Order(so_no=so_no, item_code=item_code, item_name="Widget",
                 ordered_qty=100, delivery_date=date(2026, 9, 1))


def push_actual(up: LocalStore, actual: Actual) -> str:
    raw = json.dumps(actual.to_json())
    up.list_append(book_store.ACTUALS_KEY, raw)
    return raw


# --------------------------------------------------------------------------- #
# get_store() wiring
# --------------------------------------------------------------------------- #

class TestGetStoreWiring:

    def test_overlay_is_off_without_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORE_DIR", str(tmp_path / "s"))
        monkeypatch.delenv("UPSTREAM_MONGODB_URI", raising=False)
        storage._STORE_CACHE.clear()
        assert isinstance(storage.get_store(), LocalStore)

    def test_overlay_engages_with_the_env_var(self, tmp_path, monkeypatch):
        """The upstream MongoStore is stubbed — this asserts the wiring shape,
        not that pymongo can connect."""
        built = {}

        class FakeMongo:
            def __init__(self, uri):
                built["uri"] = uri

        monkeypatch.setenv("STORE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("UPSTREAM_MONGODB_URI", "mongodb://prod/")
        monkeypatch.setattr(storage, "MongoStore", FakeMongo)
        storage._STORE_CACHE.clear()

        store = storage.get_store()

        assert isinstance(store, OverlayStore)
        assert built["uri"] == "mongodb://prod/"
        # Upstream is cached AND read-only-wrapped, in that order.
        assert isinstance(store._up, TTLCachedReads)
        assert isinstance(store._up._b, ReadOnlyStore)
        # Local half is the ordinary backend, fully writable.
        assert isinstance(store._local, LocalStore)
        storage._STORE_CACHE.clear()

    def test_local_half_is_mongo_when_both_uris_are_set(self, tmp_path, monkeypatch):
        class FakeMongo:
            def __init__(self, uri):
                self.uri = uri

        monkeypatch.setenv("MONGODB_URI", "mongodb://mine/")
        monkeypatch.setenv("UPSTREAM_MONGODB_URI", "mongodb://prod/")
        monkeypatch.setattr(storage, "MongoStore", FakeMongo)
        storage._STORE_CACHE.clear()

        store = storage.get_store()
        assert store._local.uri == "mongodb://mine/"
        assert store._up._b._b.uri == "mongodb://prod/"
        storage._STORE_CACHE.clear()


# --------------------------------------------------------------------------- #
# Clause 1, end to end
# --------------------------------------------------------------------------- #

class TestDailyEntryFlowsThrough:

    def test_a_daily_entry_saved_upstream_is_loaded_here(self, upstream):
        push_actual(upstream, an_actual("prod-1", produced=40))

        actuals = book_store.load_actuals()

        assert [a.id for a in actuals] == ["prod-1"]
        assert actuals[0].qty_produced == 40

    def test_entries_saved_here_and_upstream_both_load(self, upstream):
        push_actual(upstream, an_actual("prod-1"))
        book_store.append_actual(an_actual("mine-1", produced=7))

        assert sorted(a.id for a in book_store.load_actuals()) == ["mine-1", "prod-1"]

    def test_deleting_a_local_entry_does_not_stop_production_entries(self, upstream):
        """The behaviour clause 1 hinges on, exercised through the real delete
        path (book_store.delete_actual → list_set)."""
        push_actual(upstream, an_actual("prod-1"))
        book_store.append_actual(an_actual("mine-1"))

        book_store.delete_actual("mine-1")
        assert [a.id for a in book_store.load_actuals()] == ["prod-1"]

        push_actual(upstream, an_actual("prod-2", day="2026-08-12"))

        assert sorted(a.id for a in book_store.load_actuals()) == ["prod-1", "prod-2"]

    def test_deleting_a_production_entry_here_leaves_production_alone(self, upstream):
        raw = push_actual(upstream, an_actual("prod-1"))

        book_store.delete_actual("prod-1")

        assert book_store.load_actuals() == []
        assert upstream.list_all(book_store.ACTUALS_KEY) == [raw]

    def test_orders_added_upstream_appear_here(self, upstream):
        order = an_order()
        upstream.hset(book_store.ORDERS_KEY, "SO1\x1fITEM-A",
                      json.dumps(order.to_json()))

        loaded = book_store.load_active_orders()

        assert [o.so_no for o in loaded.values()] == ["SO1"]

    def test_an_order_completed_here_stays_out_of_the_active_list(self, upstream):
        """Mark-complete moves an order between two hash keys. Upstream still
        lists it as active; the local tombstone must win."""
        upstream.hset(book_store.ORDERS_KEY, "SO1\x1fITEM-A",
                      json.dumps(an_order().to_json()))

        assert book_store.complete_order("SO1", "ITEM-A") is True

        assert book_store.load_active_orders() == {}
        assert list(book_store.load_completed_orders()) == [("SO1", "ITEM-A")]


# --------------------------------------------------------------------------- #
# Clause 1 drives the schedule, not just the table
# --------------------------------------------------------------------------- #

def test_new_upstream_actual_busts_the_plan_cache(upstream):
    """The guarantee that makes clause 1 mean something: without it the
    duplicate would keep serving a schedule computed before the entry existed.

    ``_plan_fingerprint`` is the one that matters here — it digests the FULL
    actuals content. ``_current_book_sig`` is quantity-derived on purpose (see
    its use in the optimize trigger) and is covered separately below.
    """
    from api import main
    from engine.config import Config

    config = Config()
    before = main._plan_fingerprint(config)

    push_actual(upstream, an_actual("prod-1"))

    assert main._plan_fingerprint(config) != before


def test_upstream_actual_moves_the_book_signature_when_it_changes_quantities(upstream):
    """_current_book_sig is derived from remaining quantities, so it moves only
    when an entry actually changes what is left to make."""
    from api import main

    upstream.hset(book_store.ORDERS_KEY, "SO1\x1fITEM-A",
                  json.dumps(an_order().to_json()))
    before = main._current_book_sig()

    push_actual(upstream, an_actual("prod-1", produced=40))

    assert main._current_book_sig() != before
