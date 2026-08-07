"""The counterfactual's covariates must not encode the treatment.

`n_poi_live` is the panel-health covariate the GBM and the STGNN both read. It
exists to absorb the vendor's 27% hourly-construction slide. It must not also
tell the model that a game happened, because the whole estimand is the gap
between the observed game day and a counterfactual built from these features.

The covariate is therefore LAGGED: week W carries week W-1's count. Two other
shapes were measured and rejected. Counting the whole week leaks (+6.1%
n_poi_live on game weeks within 500m against +1.4% citywide, so 4.6 points of
venue-specific game-week signal). Counting only the week's control days leaks
harder in the opposite direction (-25.4% against -22.6%), because a game week
has fewer control days for a distinct-count union to accumulate over, and the
denominator artifact swamps the thing it was meant to remove. The lag has no
denominator artifact, every week having exactly seven days, and measures at
-1.1% against +1.1%, which is noise.
"""
from __future__ import annotations

import duckdb
import pytest

from eia_pipeline.transform import panel


@pytest.fixture()
def con():
    c = duckdb.connect()
    # week of 2024-05-27 (prior) and week of 2024-06-03 (current)
    c.sql("""
        CREATE TABLE hourly AS SELECT * FROM (VALUES
            ('p1', DATE '2024-05-28'),
            ('p2', DATE '2024-05-29'),
            ('p3', DATE '2024-06-04')
        ) t(footprint_id, date)""")
    c.sql("""
        CREATE TABLE poi_cell AS SELECT * FROM (VALUES
            ('p1', 1), ('p2', 1), ('p3', 1)
        ) t(footprint_id, unit_id)""")
    c.sql("CREATE TABLE games AS SELECT * FROM (VALUES (DATE '2024-06-05')) t(date)")
    yield c
    c.close()


def _coverage(con):
    sql = panel.coverage_sql("hourly", "poi_cell", "games")
    return {(r[0], str(r[1])): r[2] for r in con.sql(sql).fetchall()}


def test_coverage_for_a_week_is_the_previous_weeks_count(con):
    """Week W reports week W-1's distinct POIs. Two POIs reported in the week of
    2024-05-27, so the week of 2024-06-03 carries 2, not the 1 that reported in
    its own week."""
    cov = _coverage(con)
    assert cov[(1, "2024-06-03")] == 2


def test_current_week_activity_cannot_move_the_current_week_covariate(con):
    """The operational form of "carries no contemporaneous information": perturb
    anything inside week W, including its game days, and week W's value must not
    move. Counting the week itself fails this, which is how game-day activity got
    into a covariate the counterfactual is built from.
    """
    before = _coverage(con)[(1, "2024-06-03")]
    con.sql("""INSERT INTO hourly VALUES
        ('p4', DATE '2024-06-05'), ('p5', DATE '2024-06-05'),
        ('p6', DATE '2024-06-06')""")
    con.sql("INSERT INTO poi_cell VALUES ('p4', 1), ('p5', 1), ('p6', 1)")
    assert _coverage(con)[(1, "2024-06-03")] == before


def test_coverage_still_tracks_real_panel_health(con):
    """The lag must not flatten the covariate. A POI appearing in the prior week
    has to show up in the following week's value."""
    before = _coverage(con)[(1, "2024-06-03")]
    con.sql("INSERT INTO hourly VALUES ('p9', DATE '2024-05-30')")
    con.sql("INSERT INTO poi_cell VALUES ('p9', 1)")
    assert _coverage(con)[(1, "2024-06-03")] == before + 1
