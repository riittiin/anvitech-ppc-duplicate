"""OverlayStore — the one-way mirror that backs the duplicate deployment.

Two clauses are under test here:

1. Data recorded on the ORIGINAL site flows into the duplicate automatically.
2. Nothing the duplicate does can reach the original.

Every test uses two LocalStore instances — one standing in for the upstream
(production) Mongo, one for the duplicate's own Mongo.
"""
from __future__ import annotations

import json

import pytest

from engine.overlay_store import (
    NEVER_MIRRORED,
    OverlayStore,
    ReadOnlyStore,
    ReadOnlyStoreError,
)
from engine.storage import LocalStore


@pytest.fixture
def stores(tmp_path):
    """(overlay, upstream, local) — upstream is writable here so tests can
    simulate production activity; the overlay only ever sees it read-only."""
    upstream = LocalStore(tmp_path / "upstream")
    local = LocalStore(tmp_path / "local")
    return OverlayStore(ReadOnlyStore(upstream), local), upstream, local


K = "anvitech:orders"
L = "anvitech:actuals"


def actual(aid: str, qty: int = 1) -> str:
    return json.dumps({"id": aid, "good_qty": qty})


# --------------------------------------------------------------------------- #
# Clause 2 — the duplicate can never write upstream
# --------------------------------------------------------------------------- #

class TestReadOnlyUpstream:

    @pytest.mark.parametrize("call", [
        lambda s: s.kv_set("k", "v"),
        lambda s: s.hset("k", "f", "v"),
        lambda s: s.hdel("k", "f"),
        lambda s: s.list_append("k", "v"),
        lambda s: s.list_set("k", ["v"]),
        lambda s: s.delete_key("k"),
    ])
    def test_every_mutating_method_raises(self, tmp_path, call):
        ro = ReadOnlyStore(LocalStore(tmp_path / "up"))
        with pytest.raises(ReadOnlyStoreError):
            call(ro)

    def test_reads_still_work(self, tmp_path):
        backend = LocalStore(tmp_path / "up")
        backend.kv_set("k", "v")
        assert ReadOnlyStore(backend).kv_get("k") == "v"

    def test_no_overlay_operation_ever_writes_upstream(self, stores):
        """The exhaustive version: run every mutating overlay operation and
        assert the upstream files are byte-identical afterwards."""
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:masters", "M")
        upstream.hset(K, "SO1", "order1")
        upstream.list_append(L, actual("a1"))

        before = _snapshot(upstream)

        overlay.kv_set("anvitech:masters", "MINE")
        overlay.hset(K, "SO1", "edited")
        overlay.hset(K, "SO2", "new")
        overlay.hdel(K, "SO1")
        overlay.list_append(L, actual("mine"))
        overlay.list_set(L, [actual("only")])
        overlay.delete_key("anvitech:plan_config")

        assert _snapshot(upstream) == before


def _snapshot(store: LocalStore) -> dict:
    return {p.name: p.read_bytes() for p in store.base.iterdir()}


# --------------------------------------------------------------------------- #
# kv — whole-value copy-on-write
# --------------------------------------------------------------------------- #

class TestKeyValue:

    def test_reads_fall_through_to_upstream(self, stores):
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:masters", "PROD")
        assert overlay.kv_get("anvitech:masters") == "PROD"

    def test_local_write_wins_and_sticks(self, stores):
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:masters", "PROD")
        overlay.kv_set("anvitech:masters", "MINE")
        assert overlay.kv_get("anvitech:masters") == "MINE"

        upstream.kv_set("anvitech:masters", "PROD-V2")
        assert overlay.kv_get("anvitech:masters") == "MINE"

    def test_untouched_key_keeps_tracking_upstream(self, stores):
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:plan_config", "V1")
        assert overlay.kv_get("anvitech:plan_config") == "V1"
        upstream.kv_set("anvitech:plan_config", "V2")
        assert overlay.kv_get("anvitech:plan_config") == "V2"

    def test_missing_everywhere_is_none(self, stores):
        overlay, _, _ = stores
        assert overlay.kv_get("anvitech:nope") is None

    def test_empty_string_counts_as_a_local_value(self, stores):
        """'' is falsy but is a real stored value — it must shadow upstream."""
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:plan_config", "PROD")
        overlay.kv_set("anvitech:plan_config", "")
        assert overlay.kv_get("anvitech:plan_config") == ""


# --------------------------------------------------------------------------- #
# hash — per-field merge (the order book)
# --------------------------------------------------------------------------- #

class TestHashMerge:

    def test_upstream_fields_are_visible(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        upstream.hset(K, "SO2", "order2")
        assert overlay.hgetall(K) == {"SO1": "order1", "SO2": "order2"}

    def test_new_upstream_orders_keep_arriving(self, stores):
        """Clause 1 for the order book: production adds an order after the
        duplicate has already made edits of its own."""
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.hset(K, "SO2", "my-own-order")

        upstream.hset(K, "SO3", "brand-new-from-prod")

        assert overlay.hgetall(K) == {
            "SO1": "order1",
            "SO2": "my-own-order",
            "SO3": "brand-new-from-prod",
        }

    def test_local_field_shadows_upstream(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "prod-version")
        overlay.hset(K, "SO1", "my-version")
        assert overlay.hgetall(K)["SO1"] == "my-version"

        upstream.hset(K, "SO1", "prod-version-2")
        assert overlay.hgetall(K)["SO1"] == "my-version"

    def test_hdel_hides_an_upstream_field(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        upstream.hset(K, "SO2", "order2")
        overlay.hdel(K, "SO1")
        assert overlay.hgetall(K) == {"SO2": "order2"}

    def test_hdel_survives_upstream_rewriting_the_field(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.hdel(K, "SO1")
        upstream.hset(K, "SO1", "order1-updated")
        assert "SO1" not in overlay.hgetall(K)

    def test_hset_after_hdel_brings_the_field_back(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.hdel(K, "SO1")
        overlay.hset(K, "SO1", "restored")
        assert overlay.hgetall(K)["SO1"] == "restored"


# --------------------------------------------------------------------------- #
# list — per-entry merge (the Daily Entry data)
# --------------------------------------------------------------------------- #

class TestListMerge:

    def test_upstream_entries_are_visible(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        upstream.list_append(L, actual("a2"))
        assert overlay.list_all(L) == [actual("a1"), actual("a2")]

    def test_daily_entry_from_production_appears_live(self, stores):
        """Clause 1, the headline case."""
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        assert len(overlay.list_all(L)) == 1

        upstream.list_append(L, actual("a2"))          # someone at Anvitech saves
        assert overlay.list_all(L) == [actual("a1"), actual("a2")]

    def test_local_append_merges_with_upstream(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        overlay.list_append(L, actual("mine"))
        assert overlay.list_all(L) == [actual("a1"), actual("mine")]

    def test_deleting_a_local_entry_leaves_upstream_alone(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        overlay.list_append(L, actual("mine"))

        remaining = [v for v in overlay.list_all(L) if "mine" not in v]
        overlay.list_set(L, remaining)

        assert overlay.list_all(L) == [actual("a1")]

    def test_deleting_an_upstream_entry_tombstones_it(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        upstream.list_append(L, actual("a2"))

        overlay.list_set(L, [actual("a2")])

        assert overlay.list_all(L) == [actual("a2")]

    def test_tombstoned_entry_stays_gone(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        overlay.list_set(L, [])
        assert overlay.list_all(L) == []

    def test_deleting_one_entry_does_not_stop_the_sync(self, stores):
        """The regression this whole design exists to prevent: a naive
        'any write detaches the key' overlay would silently stop clause 1
        the first time a user deleted an entry in the duplicate."""
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        upstream.list_append(L, actual("a2"))

        overlay.list_set(L, [actual("a2")])            # user deletes a1 locally

        upstream.list_append(L, actual("a3"))          # production saves a new one

        assert overlay.list_all(L) == [actual("a2"), actual("a3")]

    def test_editing_an_upstream_entry_keeps_the_local_version(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1", qty=5))

        overlay.list_set(L, [actual("a1", qty=99)])

        assert overlay.list_all(L) == [actual("a1", qty=99)]

        upstream.list_append(L, actual("a2"))
        assert set(overlay.list_all(L)) == {actual("a1", qty=99), actual("a2")}

    def test_local_entries_sort_after_upstream_entries(self, stores):
        """Documented ordering rule: the merged list is upstream entries first,
        then local ones. An entry edited here therefore moves to the end.

        Actuals are grouped by item and date everywhere they are consumed, so
        position in the raw list carries no meaning — but the rule is pinned
        here so a future change to it is a deliberate decision, not a surprise.
        """
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        overlay.list_append(L, actual("mine"))
        upstream.list_append(L, actual("a2"))

        assert overlay.list_all(L) == [actual("a1"), actual("a2"), actual("mine")]

    def test_entries_without_an_id_use_content_identity(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, "plain-string-entry")
        upstream.list_append(L, "another-one")

        overlay.list_set(L, ["another-one"])

        assert overlay.list_all(L) == ["another-one"]
        upstream.list_append(L, "third")
        assert overlay.list_all(L) == ["another-one", "third"]

    def test_re_adding_a_tombstoned_entry_works(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        overlay.list_set(L, [])
        overlay.list_append(L, actual("a1"))
        assert overlay.list_all(L) == [actual("a1")]


# --------------------------------------------------------------------------- #
# delete_key — deliberate detach, and re-attach
# --------------------------------------------------------------------------- #

class TestDetach:

    def test_delete_key_hides_upstream_entirely(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        upstream.kv_set("anvitech:masters", "M")

        overlay.delete_key(K)

        assert overlay.hgetall(K) == {}

    def test_upstream_data_does_not_reappear_after_a_wipe(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.delete_key(K)
        upstream.hset(K, "SO2", "order2")
        assert overlay.hgetall(K) == {}

    def test_writes_after_a_wipe_are_purely_local(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.delete_key(K)
        overlay.hset(K, "SO9", "mine")
        assert overlay.hgetall(K) == {"SO9": "mine"}

    def test_reattach_restores_the_upstream_view(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.delete_key(K)
        overlay.reattach(K)
        assert overlay.hgetall(K) == {"SO1": "order1"}

    def test_reattach_clears_tombstones(self, stores):
        overlay, upstream, _ = stores
        upstream.list_append(L, actual("a1"))
        overlay.list_set(L, [])
        assert overlay.list_all(L) == []

        overlay.reattach(L)
        assert overlay.list_all(L) == [actual("a1")]

    def test_reattach_discards_local_edits(self, stores):
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:masters", "PROD")
        overlay.kv_set("anvitech:masters", "MINE")
        overlay.reattach("anvitech:masters")
        assert overlay.kv_get("anvitech:masters") == "PROD"

    def test_detached_keys_are_reported(self, stores):
        overlay, upstream, _ = stores
        overlay.delete_key(K)
        overlay.kv_set("anvitech:masters", "MINE")
        assert K in overlay.detached_keys()
        assert "anvitech:masters" not in overlay.detached_keys()


# --------------------------------------------------------------------------- #
# Never-mirrored keys
# --------------------------------------------------------------------------- #

class TestNeverMirrored:

    def test_session_secret_is_on_the_list(self):
        assert "anvitech:session_secret" in NEVER_MIRRORED

    def test_session_secret_never_reads_from_upstream(self, stores):
        """If the duplicate adopted production's session secret, a login cookie
        from the real site would validate on the duplicate."""
        overlay, upstream, _ = stores
        upstream.kv_set("anvitech:session_secret", "PRODUCTION-SECRET")
        assert overlay.kv_get("anvitech:session_secret") is None

    def test_session_secret_writes_and_reads_locally(self, stores):
        overlay, upstream, local = stores
        upstream.kv_set("anvitech:session_secret", "PRODUCTION-SECRET")
        overlay.kv_set("anvitech:session_secret", "MY-SECRET")
        assert overlay.kv_get("anvitech:session_secret") == "MY-SECRET"
        assert local.kv_get("anvitech:session_secret") == "MY-SECRET"


# --------------------------------------------------------------------------- #
# Overlay bookkeeping must stay invisible to the app
# --------------------------------------------------------------------------- #

class TestBookkeepingIsolation:

    def test_overlay_metadata_is_not_readable_as_an_app_key(self, stores):
        overlay, _, _ = stores
        overlay.hdel(K, "SO1")                     # writes a tombstone
        with pytest.raises(ValueError):
            overlay.kv_get("overlay:meta:" + K)

    def test_app_keys_are_unaffected_by_metadata_writes(self, stores):
        overlay, upstream, _ = stores
        upstream.hset(K, "SO1", "order1")
        overlay.hdel(K, "SO2")                     # tombstone a field that isn't there
        assert overlay.hgetall(K) == {"SO1": "order1"}
