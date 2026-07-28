"""Deterministic unit test for the dollarization step (no network)."""
from eia_pipeline.calibrate.spend_velocity import dollarize


def test_dollarize_arithmetic():
    # 1.0 pp per 10k fans, a 20k crowd -> 2.0% lift; on a $100M/day base -> $2.0M.
    d = dollarize(pp_per_10k=1.0, attendance=20000, sf_daily_spend_base_usd=100_000_000)
    assert d["lift_pct"] == 2.0
    assert d["event_day_lift_usd"] == 2_000_000
    assert d["usd_per_attendee"] == 100.0  # $2.0M / 20k fans


def test_dollarize_scales_linearly_with_base():
    a = dollarize(1.0, 20000, 50_000_000)
    b = dollarize(1.0, 20000, 100_000_000)
    assert b["event_day_lift_usd"] == 2 * a["event_day_lift_usd"]
