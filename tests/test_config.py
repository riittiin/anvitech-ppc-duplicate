"""Config knobs. The recorded-downtime→plan gate was removed (the feedback loop is
quantity-only), so the config no longer carries an apply_downtime_to_plan flag."""
from datetime import date

from engine.config import Config


def test_downtime_flag_is_gone():
    assert not hasattr(Config(), "apply_downtime_to_plan")


def test_from_dict_ignores_removed_downtime_flag():
    # A persisted config that still carries the old key must not break.
    cfg = Config.from_dict({"apply_downtime_to_plan": True, "setup_time_min": 120})
    assert not hasattr(cfg, "apply_downtime_to_plan")
    assert cfg.setup_time_min == 120


def test_to_dict_round_trips_real_knobs():
    cfg = Config(setup_time_min=75, consolidation_window_days=7)
    d = cfg.to_dict()
    assert d["setup_time_min"] == 75 and d["consolidation_window_days"] == 7
    assert "apply_downtime_to_plan" not in d


def test_expedite_window_defaults_off_and_round_trips():
    # Off by default (current plans unchanged) and survives a to_dict/from_dict trip
    # so the admin's Settings tick mark persists with the saved plan config.
    assert Config().expedite_window_min == 0
    cfg = Config.from_dict({"expedite_window_min": 45})
    assert cfg.expedite_window_min == 45
    assert Config.from_dict(cfg.to_dict()).expedite_window_min == 45


def test_balance_operator_load_defaults_off_and_round_trips():
    assert Config().balance_operator_load is False
    cfg = Config.from_dict({"balance_operator_load": True})
    assert cfg.balance_operator_load is True
    assert Config.from_dict(cfg.to_dict()).balance_operator_load is True


# --------------------------------------------------------------------------- #
# Live current-date mode: plan_start_date is nullable — None = "auto: start from
# today (IST)". The engine never sees None; the API boundary resolves it.
# --------------------------------------------------------------------------- #
def test_plan_start_date_defaults_to_none_auto():
    assert Config().plan_start_date is None


def test_none_plan_start_date_round_trips_as_json_null():
    cfg = Config()  # plan_start_date None
    d = cfg.to_dict()
    assert d["plan_start_date"] is None
    assert Config.from_dict(d).plan_start_date is None


def test_explicit_plan_start_date_round_trips():
    cfg = Config(plan_start_date=date(2025, 3, 1))
    d = cfg.to_dict()
    assert d["plan_start_date"] == "2025-03-01"
    assert Config.from_dict(d).plan_start_date == date(2025, 3, 1)


def test_empty_and_missing_plan_start_date_become_none():
    assert Config.from_dict({"plan_start_date": ""}).plan_start_date is None
    assert Config.from_dict({"plan_start_date": None}).plan_start_date is None
    assert Config.from_dict({}).plan_start_date is None


def test_validate_passes_with_none_plan_start_date():
    Config().validate()  # must not raise


def test_flexible_machines_defaults_false_and_round_trips():
    import pytest
    assert Config().flexible_machines is False
    d = Config(flexible_machines=True).to_dict()
    assert d["flexible_machines"] is True
    assert Config.from_dict(d).flexible_machines is True


def test_flexible_machines_must_be_bool():
    import pytest
    c = Config()
    c.flexible_machines = "yes"
    with pytest.raises(ValueError):
        c.validate()
