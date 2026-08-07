"""Cell-grain panels: the modelling substrate.

We build two panels rather than one because the hourly and daily source arrays are not equally trustworthy. Hourly person-hours drift by about 27% across our window because of a change in how the vendor constructs them, and daily visits do not. We measured this as the total variation between the 2023 and 2025 distributions of activity across cells: 2.9% for daily against 14.4% for hourly.

  cell_hour  Dense unit x date x hour person-hours, 2023-01-02..2025-12-31. We use this for within-day shape and for contrasts that are local in time, such as a game against control days a few weeks either side. We do not use it for cross-year levels or shares.

  cell_day   Unit x date visits over the full bronze span. This is the clean series, and it carries the level for the dollars bridge.

The bronze explode keeps only the non-zero rows, so a missing cell-hour is a zero that was never written rather than a null. We build cell_hour against a full spine of cells x dates x hours and zero-fill it, because otherwise every mean we compute downstream uses the wrong denominator and comes out inflated. We also carry n_poi_reporting so the vendor drift is something we can model rather than a confound we cannot see.
"""
from __future__ import annotations

from pathlib import Path

from ..io import duckdb_s3
from ..settings import settings

# Our hourly covariates (weather_hour, event_hour) only cover this span, so this is
# the hourly modelling window even though bronze itself reaches back to 2020.
HOURLY_START, HOURLY_END = "2023-01-02", "2025-12-31"

BASE = "data/bronze_sf"
HOURLY = f"read_parquet('{BASE}/hourly/*.parquet')"
DAILY = f"read_parquet('{BASE}/daily/*.parquet')"
POI_CELL = f"read_parquet('{BASE}/poi_cell.parquet')"

# Giants home dates, for excluding the treatment from covariate construction.
# Exhibition game types are not treatment (see pipeline/build_silver.py), but
# they are still days when the ballpark drew a crowd, so for the purpose of
# keeping game-day activity OUT of a control covariate every scheduled date
# counts.
MLB_REL = "S3/mlb_giants_schedule/mlb_giants_home_games.csv"


def game_dates() -> str:
    """Table expression yielding one `date` column of Giants home dates."""
    from ..ingest.advan_bronze import medallion_root

    src = f"{medallion_root().rstrip('/')}/{MLB_REL}"
    return f"(SELECT DISTINCT date::DATE AS date FROM read_csv('{src}', all_varchar=true))"


def coverage_sql(hourly: str, poi_cell: str, games: str) -> str:
    """unit x ISO week distinct live POIs, LAGGED one week.

    Week W carries week W-1's count, so the covariate is strictly backward
    looking and nothing inside week W, game day or not, can move it.

    Counting week W itself leaked the treatment: a POI that only reports when the
    ballpark is full made its own cell look structurally healthier, and the model
    carried that into the counterfactual it is supposed to compare the game day
    against. Measured on the real panel over 2023-04 to 2024-09, cells within
    500m ran +6.1% n_poi_live on game weeks against +1.5% citywide, so 4.6 points
    of venue-specific game-week signal sitting in a control covariate.

    Excluding the week's game days instead is WORSE and was measured before being
    rejected: -25.4% within 500m against -22.6% citywide. A distinct-count union
    grows with the number of days it spans, and a game week has fewer control
    days, so the denominator artifact is larger than the leak it removes. A
    control-day daily mean sits in between (+2.9% against +5.2%). The lag has no
    denominator artifact, every week having exactly seven days, and measures
    -1.1% against +1.1%, which is noise. `games` is therefore unused here and
    kept only so the signature documents what this covariate must stay free of.
    """
    del games  # the lag, not a filter, is what keeps the treatment out
    return f"""
        SELECT pc.unit_id,
               (date_trunc('week', h.date) + INTERVAL 7 DAY)::DATE AS week_start,
               count(DISTINCT h.footprint_id) AS n_poi_live
        FROM {hourly} h JOIN {poi_cell} pc USING (footprint_id)
        GROUP BY 1, 2
    """


def build_cell_hour(con=None, start: str = HOURLY_START, end: str = HOURLY_END) -> Path:
    """Dense unit x date x hour person-hours, plus the reporting-POI count.

    Writing per calendar year is not enough here. We need the full spine, so the
    query builds every cell x date x hour combination and left-joins the observed
    rows onto it.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "cell_hour.parquet"
    con.execute(
        f"""
        COPY (
            WITH obs AS (
                SELECT pc.unit_id, h.date, h.hour,
                       sum(h.person_hours) AS person_hours,
                       count(DISTINCT h.footprint_id) AS n_poi_reporting
                FROM {HOURLY} h JOIN {POI_CELL} pc USING (footprint_id)
                WHERE h.date BETWEEN DATE '{start}' AND DATE '{end}'
                GROUP BY 1, 2, 3
            ),
            spine AS (
                SELECT u.unit_id, d.date, hr.hour
                FROM (SELECT DISTINCT unit_id FROM {POI_CELL}) u
                CROSS JOIN (SELECT unnest(generate_series(DATE '{start}',
                                                          DATE '{end}',
                                                          INTERVAL 1 DAY))::DATE AS date) d
                CROSS JOIN (SELECT unnest(generate_series(0, 23)) AS hour) hr
            )
            SELECT s.unit_id, s.date, CAST(s.hour AS TINYINT) AS hour,
                   coalesce(o.person_hours, 0) AS person_hours,
                   coalesce(o.n_poi_reporting, 0) AS n_poi_reporting
            FROM spine s LEFT JOIN obs o USING (unit_id, date, hour)
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n, nz = con.execute(
        f"""SELECT count(*), count(*) FILTER (WHERE person_hours > 0)
            FROM read_parquet('{dest}')"""
    ).fetchone()
    print(f"  cell_hour: {n:,} rows ({100 * nz / n:.1f}% non-zero) -> {dest.name}",
          flush=True)
    return dest


def build_cell_week_coverage(con=None) -> Path:
    """unit x ISO week: how many distinct POIs in the cell reported, lagged a week.

    We cannot use n_poi_reporting at hour grain as our drift covariate. That column counts POIs with a non-zero hour, so whenever person_hours is 0 the count is 0 as well, and the feature ends up leaking the target almost deterministically. Counting the distinct POIs that were live in a cell across a whole week gives us a real measure of panel health instead, and it still tracks the same 27% construction slide without encoding any single hour's outcome.

    The count is lagged one week for the same reason we could not use the hour-grain column: read contemporaneously it let the treatment back into a covariate the counterfactual is built from. `coverage_sql` holds the measurements behind that choice, including the two alternatives that measured worse, and tests/test_panel_leakage.py pins the invariant.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "cell_week_coverage.parquet"
    con.execute(
        f"""
        COPY ({coverage_sql(HOURLY, POI_CELL, game_dates())})
        TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    print(f"  cell_week_coverage: {n:,} rows -> {dest.name}", flush=True)
    return dest


def build_cell_day(con=None) -> Path:
    """unit x date visits, with person-hours and both reporting counts alongside.

    Visits are the clean series here, so this is what the dollars bridge reads.
    We keep person-hours on the same rows for comparison, not for use as a level.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "cell_day.parquet"
    con.execute(
        f"""
        COPY (
            WITH d AS (
                SELECT pc.unit_id, x.date, sum(x.visits) AS visits,
                       count(DISTINCT x.footprint_id) AS n_poi_visits
                FROM {DAILY} x JOIN {POI_CELL} pc USING (footprint_id)
                GROUP BY 1, 2
            ),
            h AS (
                SELECT pc.unit_id, x.date, sum(x.person_hours) AS person_hours,
                       count(DISTINCT x.footprint_id) AS n_poi_hourly
                FROM {HOURLY} x JOIN {POI_CELL} pc USING (footprint_id)
                GROUP BY 1, 2
            )
            SELECT d.unit_id, d.date, d.visits, d.n_poi_visits,
                   coalesce(h.person_hours, 0) AS person_hours,
                   coalesce(h.n_poi_hourly, 0) AS n_poi_hourly
            FROM d LEFT JOIN h USING (unit_id, date)
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n, d0, d1 = con.execute(
        f"SELECT count(*), min(date), max(date) FROM read_parquet('{dest}')"
    ).fetchone()
    print(f"  cell_day: {n:,} rows, {d0}..{d1} -> {dest.name}", flush=True)
    return dest
