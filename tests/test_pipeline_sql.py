"""Predicate-level tests for the DuckDB medallion scripts.

`pipeline/` is a pair of standalone scripts, not a package, so these tests import
them by path and exercise the individual SQL predicates against tiny synthetic
tables. That is enough to pin the three things that were silently wrong: the
treatment definition swallowing exhibition games, the balanced-POI filter being
scoped to the wrong window, and a leakage gate that could not fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import build_gold  # noqa: E402
import build_silver  # noqa: E402


@pytest.fixture()
def con():
    c = duckdb.connect()
    yield c
    c.close()


# ------------------------------------------------------------------ treatment


def test_exhibition_games_are_not_treatment_days(con):
    """Spring training is not the thing being measured.

    Four Bay Bridge Series exhibitions sit in the MLB bronze (game_type 'S'), in
    late March against a control pool that is 63% deep offseason. Counting them as
    Giants home games puts four badly-matched, low-draw dates into every gold
    table that keys off `giants_home`.
    """
    con.sql("""
        CREATE TABLE mlb AS SELECT * FROM (VALUES
            (DATE '2024-03-26', 'S', 'Final',     27706, 'night'),
            (DATE '2024-04-05', 'R', 'Final',     35000, 'night'),
            (DATE '2024-10-01', 'D', 'Final',     40000, 'day')
        ) t(date, game_type, status, attendance, day_night)""")
    got = con.sql(
        f"SELECT date FROM mlb WHERE {build_silver.TREATMENT_GAME_FILTER} ORDER BY 1"
    ).fetchall()
    assert [r[0].isoformat() for r in got] == ["2024-04-05", "2024-10-01"]


def test_postseason_games_stay_in_the_treatment_set(con):
    """Division and league series games are real home games and must survive the
    exhibition filter; only 'S' is excluded."""
    con.sql("""
        CREATE TABLE mlb AS SELECT * FROM (VALUES
            (DATE '2024-10-01', 'D', 'Final', 40000, 'day'),
            (DATE '2024-10-08', 'L', 'Final', 41000, 'night'),
            (DATE '2024-10-20', 'W', 'Final', 42000, 'night'),
            (DATE '2024-03-26', 'S', 'Final', 27706, 'night')
        ) t(date, game_type, status, attendance, day_night)""")
    n = con.sql(
        f"SELECT count(*) FROM mlb WHERE {build_silver.TREATMENT_GAME_FILTER}"
    ).fetchone()[0]
    assert n == 3


# ------------------------------------------------------------- balanced panel


def test_balanced_poi_filter_is_scoped_to_the_study_window(con):
    """`visits_balanced` means "present every week we study", not "present every
    week Advan shipped".

    The extract runs 2020-01-06 to 2026-05-25; the panel does not. Requiring
    presence across the whole extract disqualifies any POI that opened after the
    COVID era, which is most of the ones that matter.
    """
    con.sql("""
        CREATE TABLE adv AS SELECT * FROM (VALUES
            ('in-window-complete', DATE '2020-01-06'),
            ('in-window-complete', DATE '2023-01-02'),
            ('in-window-complete', DATE '2023-01-09'),
            ('opened-in-2023',     DATE '2023-01-02'),
            ('opened-in-2023',     DATE '2023-01-09'),
            ('missed-a-week',      DATE '2023-01-02')
        ) t(FOOTPRINT_ID, DATE_RANGE_START)""")
    window_weeks = con.sql(
        f"""SELECT COUNT(DISTINCT DATE_RANGE_START::DATE) FROM adv
            WHERE {build_silver.window_week_filter('2023-01-01', '2023-12-31')}"""
    ).fetchone()[0]
    assert window_weeks == 2, "only the two 2023 weeks are in the study window"

    balanced = con.sql(
        f"""SELECT FOOTPRINT_ID FROM adv
            WHERE {build_silver.window_week_filter('2023-01-01', '2023-12-31')}
            GROUP BY 1 HAVING COUNT(DISTINCT DATE_RANGE_START::DATE) = {window_weeks}
            ORDER BY 1"""
    ).fetchall()
    assert [r[0] for r in balanced] == ["in-window-complete", "opened-in-2023"]


# -------------------------------------------------------------------- leakage


def test_leakage_gate_catches_a_game_day_inside_a_training_split(con):
    """The gate has to be able to fail.

    The previous predicate was `clean_control AND giants_home`, and silver defines
    clean_control as `game IS NULL` and giants_home as `game IS NOT NULL`, so it
    was identically false on every row. It reported PASS on a check that never ran.
    """
    con.sql("""
        CREATE TABLE gnn_time_hour AS SELECT * FROM (VALUES
            (DATE '2024-06-01', 'train', TRUE),
            (DATE '2025-08-01', 'test',  TRUE)
        ) t(date, split, giants_home)""")
    bad = build_gold.count_split_boundary_leaks(con, "gnn_time_hour")
    assert bad == 0

    con.sql("INSERT INTO gnn_time_hour VALUES (DATE '2025-08-02', 'train', TRUE)")
    assert build_gold.count_split_boundary_leaks(con, "gnn_time_hour") == 1


def test_leakage_gate_is_not_vacuous_on_the_real_predicate(con):
    """A row can satisfy the gate's predicate. If no row ever can, it is not a
    test, and the old one could not: it required a date to be both null and not
    null at the same time."""
    con.sql("""
        CREATE TABLE gnn_time_hour AS SELECT * FROM (VALUES
            (DATE '2024-06-01', 'train', TRUE)
        ) t(date, split, giants_home)""")
    con.sql("INSERT INTO gnn_time_hour VALUES (DATE '2026-01-01', 'train', TRUE)")
    assert build_gold.count_split_boundary_leaks(con, "gnn_time_hour") > 0


# ------------------------------------------- the same rule in the serve lineage


def test_serving_spine_uses_the_same_exhibition_filter():
    """`pipeline/` and `src/eia_pipeline/` build the treatment independently.

    Gold supplies `giants_home` for the training window, so the medallion fix
    reaches the model panel on its own, but the 2026 extension in
    `serve/spine_2026.py` reads the MLB bronze directly. If only one side filters
    exhibitions, the website marks next spring's Bay Bridge Series as a Giants home
    game while the panel it was fitted on does not.
    """
    from eia_pipeline.serve import spine_2026 as sp

    assert set(sp.EXHIBITION_GAME_TYPES) == set(build_silver.EXHIBITION_GAME_TYPES)
    assert sp.TREATMENT_GAME_FILTER == build_silver.TREATMENT_GAME_FILTER


def test_serving_spine_filters_exhibitions_at_every_mlb_read(tmp_path):
    """Every read of the MLB bronze in the serve lineage has to carry the filter:
    one that does not silently re-admits the four exhibition dates."""
    from pathlib import Path

    src = Path("src/eia_pipeline/serve/spine_2026.py").read_text()
    reads = [ln for ln in src.splitlines() if "_src(MLB)" in ln]
    assert reads, "expected at least one MLB read in the serving spine"
    for ln in reads:
        idx = src.splitlines().index(ln)
        window = "\n".join(src.splitlines()[idx:idx + 6])
        assert "TREATMENT_GAME_FILTER" in window or "game_type" in window, (
            f"unfiltered MLB read near line {idx + 1}: {ln.strip()}")
