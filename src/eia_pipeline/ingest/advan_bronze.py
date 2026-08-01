"""Citywide SF presence from RAW Advan weekly patterns (bronze layer).

Why this exists when `silver/occupancy_poi_hour` already has hourly presence:
silver is capped at 5km from Oracle Park (12,095 POIs) and starts 2023. Bronze
covers the whole city (17,826 SF POIs, 334 weeks from 2020-01-06), which is what
lets presence be calibrated against CDTFA, whose grain is the city (SF is a
consolidated city-county, so county grain = city grain). See the rev-2 design in
docs/superpowers/specs/2026-07-31-citywide-spend-nowcast-design.md.

TWO ARRAYS, TWO UNITS. Do not mix them:
  VISITS_BY_DAY       7 elements   -> VISITS (reconciles to VISIT_COUNTS)
  VISITS_BY_EACH_HOUR 168 elements -> PERSON-HOURS (a visitor spanning 3 hours is counted in all 3, so this is ~4x visits)

Both are VARCHAR JSON in bronze and need `from_json`. Weeks start MONDAY (verified:
all 334 DATE_RANGE_START values are Mondays), so for a 1-based array index i:
    day  = DATE_RANGE_START + (i-1) // 24 days      (hourly; (i-1) for daily)
    hour = (i-1) % 24

We check this mapping with verify_alignment() before trusting anything downstream. Silver was built from this same bronze by the team pipeline, so if our explode is right the two should match exactly, and an off-by-one would otherwise look perfectly plausible in every chart we draw from it.
"""
from __future__ import annotations

from pathlib import Path

from ..io import duckdb_s3
from ..settings import settings

# Advan's own city label. Measured 2026-07-31: 17,826 POIs over 334 weeks.
SF_CITY_FILTER = "CITY ILIKE 'San Francisco'"

HOURS_PER_WEEK = 168
DAYS_PER_WEEK = 7


def medallion_uri(layer: str, *parts: str) -> str:
    """AWS bucket pull is structured as: s3://{S3_BUCKET}/{layer}/{parts...}. We build the path here rather than using settings.s3_uri(), because that helper prepends our own eia-nowcast/ prefix and the bronze, silver and gold layers sit alongside that prefix rather than inside it. The bucket name still comes from the environment; only the layer name is written literally. We raise here if S3_BUCKET is unset so the failure shows up at config time instead of as a confusing read error later.
    """
    if not settings.s3_bucket:
        raise RuntimeError("S3_BUCKET not set. See .env.example")
    key = "/".join([layer, *parts]).strip("/")
    return f"s3://{settings.s3_bucket}/{key}"


def bronze_patterns() -> str:
    """Glob for the raw weekly-patterns drop.

    This is a function rather than a module constant on purpose. Building the URI
    at import time means the module cannot be imported at all without a configured
    S3_BUCKET, which breaks a fresh checkout and blocks pytest from even collecting
    the tests. Resolving it lazily keeps the config error where it belongs, at the
    point where we actually try to read.
    """
    return medallion_uri("bronze", "advan_weekly_patterns", "*.parquet")


def silver_occupancy() -> str:
    """Path to the team's silver POI-hour table, used by verify_alignment()."""
    return medallion_uri("silver", "occupancy_poi_hour.parquet")


def verify_alignment(con=None, week: str = "2024-07-01") -> dict:
    """As I was conducting this in my personal environment, I added a verification step to ensure that I was working with both silver and bronze results as they are in S3.

    Returns a dict of counts; `ok` is True only on an exact reproduction.
    """
    con = con or duckdb_s3()
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _mine AS
        WITH s AS (
            SELECT FOOTPRINT_ID AS fid,
                   from_json(VISITS_BY_EACH_HOUR, '["BIGINT"]') AS h
            FROM read_parquet('{bronze_patterns()}')
            WHERE CAST(DATE_RANGE_START AS DATE) = DATE '{week}'
              AND VISITS_BY_EACH_HOUR IS NOT NULL
        ),
        e AS (SELECT fid, generate_subscripts(h, 1) AS i, unnest(h) AS vh FROM s)
        SELECT fid,
               CAST(DATE '{week}' + INTERVAL ((i - 1) / 24) DAY AS DATE) AS d,
               ((i - 1) % 24) AS hr,
               vh
        FROM e
        WHERE vh > 0
        """
    )
    row = con.execute(
        f"""
        WITH sv AS (
            SELECT footprint_id AS fid, date AS d, hour AS hr, visitor_hours AS vh
            FROM read_parquet('{silver_occupancy()}')
            WHERE date BETWEEN DATE '{week}' AND DATE '{week}' + 6
        ),
        mn AS (SELECT * FROM _mine WHERE fid IN (SELECT DISTINCT fid FROM sv))
        SELECT (SELECT count(*) FROM mn) AS mine_rows,
               (SELECT count(*) FROM sv) AS silver_rows,
               (SELECT count(*) FROM mn m JOIN sv s
                  ON s.fid = m.fid AND s.d = m.d AND s.hr = m.hr
                 WHERE m.vh = s.vh) AS exact_equal,
               (SELECT count(*) FROM mn m ANTI JOIN sv s
                  ON s.fid = m.fid AND s.d = m.d AND s.hr = m.hr) AS only_mine,
               (SELECT count(*) FROM sv s ANTI JOIN mn m
                  ON s.fid = m.fid AND s.d = m.d AND s.hr = m.hr) AS only_silver
        """
    ).fetchone()
    out = dict(
        zip(("mine_rows", "silver_rows", "exact_equal", "only_mine", "only_silver"), row)
    )
    out["ok"] = (
        out["only_mine"] == 0
        and out["only_silver"] == 0
        and out["exact_equal"] == out["silver_rows"]
        and out["silver_rows"] > 0
    )
    return out


def _explode_sql(array_col: str, elems: int, value_name: str) -> str:
    """Shared explode. elems==168 -> hourly person-hours; elems==7 -> daily visits."""
    hour_expr = "((i - 1) % 24)" if elems == HOURS_PER_WEEK else "0"
    day_expr = "(i - 1) / 24" if elems == HOURS_PER_WEEK else "(i - 1)"
    return f"""
        WITH s AS (
            SELECT FOOTPRINT_ID AS footprint_id,
                   CAST(DATE_RANGE_START AS DATE) AS week_start,
                   from_json({array_col}, '["BIGINT"]') AS arr
            FROM read_parquet('{bronze_patterns()}')
            WHERE {SF_CITY_FILTER} AND {array_col} IS NOT NULL
              AND CAST(DATE_RANGE_START AS DATE) >= DATE '{{start}}'
              AND CAST(DATE_RANGE_START AS DATE) <= DATE '{{end}}'
        ),
        e AS (
            SELECT footprint_id, week_start,
                   generate_subscripts(arr, 1) AS i, unnest(arr) AS v
            FROM s
        )
        SELECT footprint_id,
               CAST(week_start + INTERVAL ({day_expr}) DAY AS DATE) AS date,
               CAST({hour_expr} AS TINYINT) AS hour,
               v AS {value_name}
        FROM e
        WHERE v > 0
    """


def poi_dimension(con=None) -> Path:
    """Land one row per SF POI in duckDB: coordinates, NAICS, category, coverage span.

    Kept OUT of the fact tables deliberately, carrying lat/lon/naics on ~141M rows is pure bloat. Everything spatial (grid cell, neighbourhood, distance to venue) is derived from this dimension and joined on footprint_id, which also makes the spatial unit a swappable lookup rather than a baked-in choice.

    Coordinates are taken from the POI's most recent week: a handful of POIs get re-geocoded across 334 weeks, and last-known is the right tie-break.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "poi_dim.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY (
            SELECT footprint_id, lat, lon, naics, top_category, sub_category, location_name, poi_cbg, weeks_present, first_week, last_week
            FROM (
                SELECT FOOTPRINT_ID AS footprint_id,
                       LATITUDE AS lat, LONGITUDE AS lon,
                       NAICS_CODE AS naics, TOP_CATEGORY AS top_category,
                       SUB_CATEGORY AS sub_category, LOCATION_NAME AS location_name,
                       POI_CBG AS poi_cbg,
                       count(*)          OVER w AS weeks_present,
                       min(CAST(DATE_RANGE_START AS DATE)) OVER w AS first_week,
                       max(CAST(DATE_RANGE_START AS DATE)) OVER w AS last_week,
                       row_number()      OVER (PARTITION BY FOOTPRINT_ID
                                               ORDER BY DATE_RANGE_START DESC) AS rn
                FROM read_parquet('{bronze_patterns()}')
                WHERE {SF_CITY_FILTER}
                WINDOW w AS (PARTITION BY FOOTPRINT_ID)
            )
            WHERE rn = 1
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    print(f"  poi_dim: {n:,} SF POIs -> {dest.name}", flush=True)
    return dest


def explode_hourly(con=None, start: str = "2020-01-06", end: str = "2026-05-25") -> Path:
    """Land citywide POI x date x hour PERSON-HOURS to data/bronze_sf/hourly/.

    Written per calendar year so a rerun can resume and files stay openable.
    """
    return _run(con, "hourly", _explode_sql("VISITS_BY_EACH_HOUR", HOURS_PER_WEEK,
                                            "person_hours"), start, end)


def explode_daily(con=None, start: str = "2020-01-06", end: str = "2026-05-25") -> Path:
    """Land citywide POI x date VISITS to data/bronze_sf/daily/."""
    return _run(con, "daily", _explode_sql("VISITS_BY_DAY", DAYS_PER_WEEK, "visits"),
                start, end)


def _run(con, kind: str, sql_template: str, start: str, end: str) -> Path:
    con = con or duckdb_s3()
    out_dir = settings.data_dir / "bronze_sf" / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    for year in range(int(start[:4]), int(end[:4]) + 1):
        y0, y1 = max(f"{year}-01-01", start), min(f"{year}-12-31", end)
        if y0 > y1:
            continue
        dest = out_dir / f"year={year}.parquet"
        sql = sql_template.format(start=y0, end=y1)
        con.execute(f"COPY ({sql}) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        print(f"  {kind} {year}: {n:,} rows -> {dest.name}", flush=True)
    return out_dir
