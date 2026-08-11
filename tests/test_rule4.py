"""Rule 4 — occupancy = cycle x qty + setup."""
from engine.config import Config
from engine.rules import rule4_setup_time


def test_occupancy_math():
    cfg = Config()  # setup 90
    # 40 min cycle x 10 pcs + 90 setup = 490.
    assert rule4_setup_time.occupancy_minutes(40, 10, cfg) == 490


def test_missing_cycle_counts_as_setup_only():
    cfg = Config()
    assert rule4_setup_time.occupancy_minutes(None, 10, cfg) == 90


def test_configurable_setup():
    cfg = Config(setup_time_min=0)
    assert rule4_setup_time.occupancy_minutes(5, 3, cfg) == 15
