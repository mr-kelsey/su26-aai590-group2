"""The three invariants whose violation is silent. No network, local fixtures only.

We picked these three because each one, if broken, produces output that looks entirely plausible and would not be caught by reading the code:

  1. day alignment      an off-by-one shifts every event effect by a day, and every chart still renders
  2. trailing baseline  a baseline that peeks forward inflates every held-out metric, and the model just looks good
  3. dense spine        averaging a sparse table without zero-filling divides by the wrong denominator and inflates every mean

We deliberately do not assert effect sizes, MAEs or dollar figures. Those move with the data, and pinning them would turn this suite into a change-detector that everyone learns to ignore. These assert the structural properties that make those numbers mean anything.
"""
import datetime as dt
import types

import duckdb
import polars as pl
import pytest

from eia_pipeline.ingest import advan_bronze as ab


# ---------------------------------------------------------------- 1. alignment

def _bronze_fixture(tmp_path, week_start="2024-07-01"):
    """One POI, one Monday-start week, hour i carrying the value i.

    Encoding the index INTO the value is what makes the mapping checkable: the
    exploded row for (date, hour) must carry exactly the array slot it came from.
    """
    n = ab.HOURS_PER_WEEK
    df = pl.DataFrame({
        "FOOTPRINT_ID": [1],
        "CITY": ["San Francisco"],
        "DATE_RANGE_START": [dt.datetime.fromisoformat(week_start)],
        "VISITS_BY_EACH_HOUR": ["[" + ",".join(str(i) for i in range(1, n + 1)) + "]"],
        "VISITS_BY_DAY": ["[1,2,3,4,5,6,7]"],
    })
    p = tmp_path / "wk.parquet"
    df.write_parquet(p)
    return p


def test_hourly_explode_maps_index_to_day_and_hour(tmp_path, monkeypatch):
    p = _bronze_fixture(tmp_path)
    monkeypatch.setattr(ab, "bronze_patterns", lambda: str(p))
    monkeypatch.setattr(ab, "settings", types.SimpleNamespace(data_dir=tmp_path))

    ab.explode_hourly(duckdb.connect(), start="2024-07-01", end="2024-07-07")
    got = pl.read_parquet(tmp_path / "bronze_sf" / "hourly" / "year=2024.parquet")

    assert got.height == ab.HOURS_PER_WEEK          # every slot survives
    for row in got.iter_rows(named=True):
        i = row["person_hours"]                      # value == 1-based array slot
        assert row["date"].day == 1 + (i - 1) // 24, f"slot {i} landed on wrong day"
        assert row["hour"] == (i - 1) % 24, f"slot {i} landed on wrong hour"


def test_daily_explode_is_seven_consecutive_days(tmp_path, monkeypatch):
    p = _bronze_fixture(tmp_path)
    monkeypatch.setattr(ab, "bronze_patterns", lambda: str(p))
    monkeypatch.setattr(ab, "settings", types.SimpleNamespace(data_dir=tmp_path))

    ab.explode_daily(duckdb.connect(), start="2024-07-01", end="2024-07-07")
    got = pl.read_parquet(tmp_path / "bronze_sf" / "daily" / "year=2024.parquet").sort("date")

    assert got.height == ab.DAYS_PER_WEEK
    assert got["visits"].to_list() == [1, 2, 3, 4, 5, 6, 7]   # slot order preserved
    assert got["hour"].unique().to_list() == [0]              # daily rows carry hour 0
    days = got["date"].to_list()
    assert (days[-1] - days[0]).days == 6                     # contiguous, no gap


# ------------------------------------------------- 2. baseline is trailing-only

def test_rolling_baseline_never_sees_the_future_or_an_event_day():
    """The ASOF construction must use only PRIOR control days.

    Built as a bare query rather than through transform.features so the test does
    not need the 11.9M-row panel. The window clauses are the ones that module uses.
    """
    con = duckdb.connect()
    # 12 daily rows for one cell/hour; days 5 and 9 are events with a huge spike
    con.execute("""
        CREATE TABLE panel AS
        SELECT 'c1' AS unit_id, DATE '2024-01-01' + INTERVAL (i) DAY AS date,
               18 AS hour, 0 AS dow,
               CASE WHEN i IN (5, 9) THEN 100.0 ELSE i * 1.0 END AS y,
               (i NOT IN (5, 9)) AS clean_control_strict
        FROM range(0, 12) t(i)
    """)
    out = con.execute("""
        WITH ctl AS (
            SELECT unit_id, date, hour, dow,
                   avg(y) OVER (PARTITION BY unit_id, dow, hour ORDER BY date
                                ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS base_k2
            FROM panel WHERE clean_control_strict
        )
        SELECT p.date, p.y, c.base_k2, c.date AS src_date
        FROM panel p ASOF LEFT JOIN ctl c
          ON p.unit_id = c.unit_id AND p.hour = c.hour AND p.dow = c.dow
         AND p.date > c.date
        ORDER BY p.date
    """).pl()

    src = out.drop_nulls("src_date")
    assert (src["src_date"] < src["date"]).all(), "baseline used a same-day or future row"
    # the spike on the event days must not reach any baseline
    assert out["base_k2"].max() < 100.0, "an event day leaked into the baseline"
    assert out["base_k2"].null_count() == 1, "only the first row should lack a baseline"


# ------------------------------------------------------------- 3. dense spine

def test_cell_hour_spine_is_dense_and_zero_filled():
    """Missing cell-hours are unwritten ZEROS, not nulls.

    Bronze stores non-zero rows only. Averaging the sparse table directly divides
    by the count of PRESENT rows instead of all hours, which inflates every mean,
    the reason panel.build_cell_hour joins onto a full spine.
    """
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE obs AS SELECT * FROM (VALUES
            ('c1', DATE '2024-01-01', 10, 5.0),
            ('c1', DATE '2024-01-02', 11, 7.0),
            ('c2', DATE '2024-01-01', 10, 3.0)
        ) t(unit_id, date, hour, person_hours)
    """)
    dense = con.execute("""
        WITH spine AS (
            SELECT u.unit_id, d.date, h.hour
            FROM (SELECT DISTINCT unit_id FROM obs) u
            CROSS JOIN (SELECT unnest(generate_series(DATE '2024-01-01',
                                                      DATE '2024-01-02',
                                                      INTERVAL 1 DAY))::DATE AS date) d
            CROSS JOIN (SELECT unnest(generate_series(0, 23)) AS hour) h
        )
        SELECT s.unit_id, s.date, s.hour,
               coalesce(o.person_hours, 0) AS person_hours
        FROM spine s LEFT JOIN obs o USING (unit_id, date, hour)
    """).pl()

    assert dense.height == 2 * 2 * 24, "spine must be cells x days x 24"
    assert dense["person_hours"].null_count() == 0, "gaps must be 0, never null"

    sparse_mean = con.execute("SELECT avg(person_hours) FROM obs").fetchone()[0]
    dense_mean = float(dense["person_hours"].mean())
    assert sparse_mean == pytest.approx(5.0)      # 15/3, the wrong denominator
    assert dense_mean == pytest.approx(15 / 96)   # 15/96, the right one
    assert sparse_mean > 30 * dense_mean          # scale of the error if skipped
