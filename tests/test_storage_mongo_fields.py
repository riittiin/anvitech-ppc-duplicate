"""Regression: MongoStore hash-field names must be safe to interpolate into a
dotted update path (``h.<field>``).

The order book keys orders by the ``"<so_no>\\x1f<item_code>"`` composite string.
Real item codes contain dots (e.g. ``61243661-01..``); a raw dot in a Mongo update
path is read as a nested-document separator and empty segments throw 'empty field
name', which broke uploads on the live (MongoDB) store. Field names are therefore
percent-encoded before they touch the path and decoded on read."""
from engine.storage import MongoStore

# The exact shapes seen in the real workbook: SO#\x1fitem, item codes with dots,
# and the pathological trailing '..'.
CASES = [
    "26-27SO12\x1f61248811-01",
    "26-27SO13\x1f61243661-01..",
    "SO-001\x1fSAMP-A-01",
    "plainsonodots",
    "$weird.field",
]


def test_encoded_field_is_mongo_path_safe():
    for f in CASES:
        enc = MongoStore._enc_field(f)
        assert "." not in enc, f"dot leaked into path for {f!r}: {enc!r}"
        assert "\x1f" not in enc, f"separator leaked into path for {f!r}"
        assert not enc.startswith("$"), f"leading $ (operator) for {f!r}: {enc!r}"


def test_field_encode_round_trips():
    for f in CASES:
        assert MongoStore._dec_field(MongoStore._enc_field(f)) == f


def test_plain_ascii_fields_are_untouched():
    # Old data was keyed by a bare SO number (no dots) — it must decode unchanged so
    # pre-existing orders still load after the fix.
    assert MongoStore._enc_field("26-27SO12") == "26-27SO12"
    assert MongoStore._dec_field("26-27SO12") == "26-27SO12"
