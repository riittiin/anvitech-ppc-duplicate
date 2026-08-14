import json

from cp_engine import genome


def _g():
    return {
        "ranks": {"SO1\x1fITEM": 0, "SO2\x1fITEM": 1},
        "cp_machine_of": {("B1", 1): "CNC1", ("B1", 2): "MD1"},
        "cp_roster": {("CNC1", 0): "Narayan", ("CNC1", 1): "Sidhu"},
        "cp_overlap_of": {"B1": 80},
        "cp_completion": {"B1": "2026-09-04"},
        "cp_solved_book_sig": "abc123",
        # ABSOLUTE ISO datetime strings, never minutes from the plan start: the
        # stored plan clock advances between the solve and the replay, and a
        # relative offset would slide the whole plan by however far it moved.
        "cp_start_of": {("B1", 1): "2026-08-12T08:00:00",
                        ("B1", 2): "2026-08-12T17:50:00"},
        # WHO the solver put at a bench. Rule 1 rosters CNC/VMC only, so
        # ``cp_roster`` names nobody there and this is the only record of it.
        "cp_bench_of": {("B1", 2): "Sandeep"},
    }


def test_the_genome_round_trips_through_json_with_tuple_keys_intact():
    """Tuple keys are not JSON-representable and a silent str() would make the
    replay look up a key that can never match — the plan would then quietly fall
    back to unassigned for every op."""
    got = genome.from_json(json.loads(json.dumps(genome.to_json(_g()))))
    assert got == _g()


def test_every_documented_key_survives_the_round_trip():
    assert set(genome.KEYS) == set(_g())
    assert set(genome.from_json(genome.to_json(_g()))) == set(genome.KEYS)


def test_an_empty_genome_round_trips_to_an_empty_genome():
    assert genome.from_json(genome.to_json({})) == {}


def test_an_unknown_key_is_dropped_rather_than_carried():
    """Forward compatibility in the safe direction: a genome written by a NEWER
    version must not smuggle a key this version will not honour into the replay."""
    raw = genome.to_json(_g())
    raw["cp_future_thing"] = {"x": 1}
    assert "cp_future_thing" not in genome.from_json(raw)
