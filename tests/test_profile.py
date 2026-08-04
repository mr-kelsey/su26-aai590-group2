"""Tests for the panel profile.

The arithmetic is not the interesting part, because polars can be trusted to count nulls.
What we want pinned is the structural/gap split, since that is a claim about our panel
rather than a computation. If a design predicate stops explaining the nulls we wrote it
for, the profile should say so loudly rather than quietly reclassify them.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from eia_pipeline.eda import profile as prof


def _panel() -> pl.LazyFrame:
    """Four rows: two game hours, two not. `first_pitch_hour` is null exactly on the
    non-game rows (structural); `temp_hr` has one null that no predicate explains."""
    return pl.LazyFrame(
        {
            "date": [dt.date(2024, 4, 6)] * 2 + [dt.date(2024, 4, 7)] * 2,
            "hour": [18, 19, 18, 19],
            "giants_home": [True, True, False, False],
            "first_pitch_hour": [18, 18, None, None],
            "temp_hr": [60.0, None, 58.0, 57.0],
            "person_hours": [100.0, 0.0, 0.0, 40.0],
            "n_poi_reporting": [3, 0, 0, 1],
            "n_poi": [10, 10, 10, 10],
            "split": ["train", "train", "test", "test"],
        }
    )


def test_structural_nulls_are_attributed_to_the_design():
    m = prof.missingness(_panel())
    fp = m.filter(pl.col("column") == "first_pitch_hour")
    assert fp["n_null"][0] == 2
    assert fp["structural"][0] == 2
    assert fp["gap"][0] == 0
    assert fp["reason"][0] == "no game that day"


def test_a_null_no_predicate_covers_is_reported_as_a_gap():
    m = prof.missingness(_panel())
    t = m.filter(pl.col("column") == "temp_hr")
    assert t["structural"][0] == 0
    assert t["gap"][0] == 1
    assert t["reason"][0] == "no design predicate"


def test_a_structural_null_outside_its_predicate_becomes_a_gap():
    """The guard that matters. We flip one game row's first_pitch_hour to null, and the
    profile has to count it as a gap rather than absorb it into the structural bucket.
    """
    lf = _panel().with_columns(
        pl.when(pl.col("hour") == 19)
        .then(None)
        .otherwise(pl.col("first_pitch_hour"))
        .alias("first_pitch_hour")
    )
    fp = prof.missingness(lf).filter(pl.col("column") == "first_pitch_hour")
    assert fp["n_null"][0] == 3
    assert fp["structural"][0] == 2
    assert fp["gap"][0] == 1


def test_columns_without_nulls_are_omitted():
    assert "person_hours" not in prof.missingness(_panel())["column"].to_list()


def test_target_reports_zero_share_and_the_log_transform():
    t = prof.target(_panel())
    assert t["zeros"] == 2
    assert t["zero_rate"] == 0.5
    assert t["log_skew"] != t["skew"]


def test_splits_are_ordered_by_date_and_carry_coverage():
    s = prof.splits(_panel())
    assert s["split"].to_list() == ["train", "test"]
    assert s["coverage"][0] == pytest.approx(0.15)  # 3 of 20 POI-hours reporting


def test_report_writes_and_names_its_own_generator(tmp_path):
    if not prof.PANEL.exists():
        pytest.skip("model_hour.parquet not present locally")
    out = prof.write_report(tmp_path / "profile.md")
    text = out.read_text()
    assert "eia_pipeline.eda.profile" in text
    for heading in ("## The target", "## Missingness", "## Splits"):
        assert heading in text
