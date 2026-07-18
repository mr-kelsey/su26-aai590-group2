#!/usr/bin/env python3
"""
build_silver.py - build the SILVER layer (conformed, joined, analysis-grain
tables) from the BRONZE layer (raw source pulls) for the Oracle Park ring
event-study.

Medallion convention for this project:
  bronze/  raw source pulls, byte-true, hand-gathered (see bronze/README.md)
  silver/  conformed + joined tables, built ONLY by this script, disposable
  gold/    model-ready / reported outputs (downstream of silver)

Silver tables written to --out:
  poi_rings.parquet           POI grain: distance + ring assignment for every
                              Advan POI (FOOTPRINT_ID key)
  visits_ring_day.parquet     ring x day: daily visits expanded from Advan
                              VISITS_BY_DAY, plus ex-venue (stadium's own POI
                              removed), food-services (NAICS 722), and
                              balanced-POI-panel variants
  calendar_day.parquet        day grain: Giants games (treatment), park /
                              Chase / Moscone / citywide event flags,
                              clean-control flag
  weather_day.parquet         day grain: NOAA downtown-first, SFO fallback
  transit_day.parquet         day grain: BART daily exits (EMBR/MONT) +
                              hourly-OD-derived arrival features where the
                              hourly data exists
  bikeshare_ring_day.parquet  ring x day: Bay Wheels trip starts/ends
                              (requires a LOCAL bronze path; skipped on s3://)
  panel_ring_day.parquet      ring x day: everything joined - THE deliverable
  build_manifest.json         provenance: params, row counts, QA results,
                              bronze snapshot, git SHA, build time
  README.md                   table docs + this build's provenance summary

Full rebuild every run; no incremental state. Same bronze in, same silver out.
QA gates fail the build loudly rather than let a wrong panel through.

Run (Steve's machine, local bronze staging, ~minutes):
  cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2 && \
    /Users/Steve3/Projects/personal/capstone/notebooks/.venv/bin/python \
    pipeline/build_silver.py \
    --bronze /Users/Steve3/Projects/personal/capstone/S3 \
    --out /Users/Steve3/Projects/personal/capstone/silver

Then publish (bronze never uses --delete; silver ALWAYS does - it is derived):
  aws s3 sync /Users/Steve3/Projects/personal/capstone/silver \
    s3://aai-590-group2-capstone/silver --delete --region us-east-2

Anyone else: point --bronze at s3://aai-590-group2-capstone/bronze (needs AWS
credentials; the bikeshare step is skipped on s3 bronze until you sync the
zips locally).

Dependencies: duckdb (pip install duckdb). Everything else is stdlib.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

import duckdb

# ---------------------------------------------------------------- parameters

ORACLE_PARK = (37.7786, -122.3893)  # lat, lon

# Ring edges in miles. 0.1864 mi = 300 m: Luke's core ring, kept as ring 1
# inside the build-plan's 0-0.5 / 0.5-1 / 1-2 / 2-5 mi set. Change HERE only.
RING_EDGES_MI = [0.0, 0.1864, 0.5, 1.0, 2.0, 5.0]
RING_LABELS = ["0-300m", "300m-0.5mi", "0.5-1mi", "1-2mi", "2-5mi"]

PANEL_START = "2022-01-01"  # study window: 2022+ era (Advan starts 2020-01-06)
PANEL_END = "2025-12-31"    # study window cutoff: full seasons 2022-2025 only
                            # (decided 2026-07-18). Bronze retains later raw
                            # data; the scope is enforced HERE, never by
                            # trimming raw files.
FOOD_NAICS_PREFIX = "722"   # Food Services and Drinking Places
VENUE_POI_NAME = "Oracle Park"  # the stadium's own Advan POI (excluded in *_ex_venue)
SF8_STATIONS = ("EMBR", "MONT", "POWL", "CIVC", "16TH", "24TH", "GLEN", "BALB")

# Pre-wired confounder drop-in: when Luke's street-fair calendar lands in
# bronze under this name (columns: date,name), the next rebuild picks it up
# with no code change.
STREET_FAIR_REL = "competing_events/street_fairs.csv"

QA = {}  # gate name -> result, written into build_manifest.json


# ------------------------------------------------------------------- helpers

def log(msg):
    print(f"[build_silver] {msg}", flush=True)


def gate(name, ok, detail):
    QA[name] = {"ok": bool(ok), "detail": detail}
    log(f"QA {'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        sys.exit(f"QA gate failed: {name}: {detail}")


def haversine_sql(lat_col, lon_col):
    lat0, lon0 = ORACLE_PARK
    return (
        f"2 * 3958.8 * asin(sqrt("
        f"sin(radians({lat_col} - ({lat0})) / 2) ^ 2 + "
        f"cos(radians({lat0})) * cos(radians({lat_col})) * "
        f"sin(radians({lon_col} - ({lon0})) / 2) ^ 2))"
    )


def ring_case_sql(dist_col):
    parts = []
    for i, label in enumerate(RING_LABELS):
        parts.append(
            f"WHEN {dist_col} < {RING_EDGES_MI[i + 1]} THEN {i + 1}"
        )
    return f"CASE WHEN {dist_col} < {RING_EDGES_MI[0]} THEN NULL " + " ".join(parts) + " ELSE NULL END"


def ring_label_sql(ring_id_col):
    whens = " ".join(
        f"WHEN {i + 1} THEN '{label}'" for i, label in enumerate(RING_LABELS)
    )
    return f"CASE {ring_id_col} {whens} END"


def bronze_path(bronze, rel):
    return f"{bronze.rstrip('/')}/{rel}"


def can_read(con, path):
    try:
        con.sql(f"SELECT 1 FROM '{path}' LIMIT 1")
        return True
    except Exception:
        return False


def csv_columns(con, path):
    rows = con.sql(f"DESCRIBE SELECT * FROM read_csv('{path}')").fetchall()
    return {r[0] for r in rows}


def git_sha():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or None
    except Exception:
        return None


def bronze_snapshot(bronze):
    """File count + bytes per top-level bronze folder (local paths only)."""
    if bronze.startswith("s3://") or not os.path.isdir(bronze):
        return {"note": "s3 bronze; snapshot skipped"}
    snap = {}
    for entry in sorted(os.listdir(bronze)):
        p = os.path.join(bronze, entry)
        if os.path.isdir(p):
            files = [
                os.path.join(dp, f)
                for dp, _, fs in os.walk(p) for f in fs if not f.startswith(".")
            ]
            snap[entry] = {"files": len(files),
                           "bytes": sum(os.path.getsize(f) for f in files)}
    return snap


# ----------------------------------------------------------------- the build

def build_poi_rings(con, bronze):
    advan = bronze_path(bronze, "advan_weekly_patterns/*.parquet")
    dist = haversine_sql("lat", "lon")
    con.sql(f"""
        CREATE OR REPLACE TABLE poi_rings AS
        WITH pois AS (
            SELECT FOOTPRINT_ID,
                   any_value(LOCATION_NAME) AS location_name,
                   any_value(LATITUDE)  AS lat,
                   any_value(LONGITUDE) AS lon,
                   any_value(NAICS_CODE) AS naics_code,
                   any_value(TOP_CATEGORY) AS top_category,
                   COUNT(*) AS weeks_present,
                   MIN(DATE_RANGE_START::DATE) AS first_week,
                   MAX(DATE_RANGE_START::DATE) AS last_week
            FROM read_parquet('{advan}')
            GROUP BY FOOTPRINT_ID),
        d AS (SELECT *, {dist} AS dist_mi FROM pois)
        SELECT * EXCLUDE (dist_mi),
               ROUND(dist_mi, 4) AS dist_mi,
               {ring_case_sql('dist_mi')} AS ring_id,
               {ring_label_sql(ring_case_sql('dist_mi'))} AS ring
        FROM d
    """)
    n_total, n_ringed = con.sql(
        "SELECT COUNT(*), COUNT(ring_id) FROM poi_rings").fetchone()
    per_ring = con.sql(
        "SELECT ring, COUNT(*) FROM poi_rings WHERE ring IS NOT NULL "
        "GROUP BY ring ORDER BY min(ring_id)").fetchall()
    log(f"poi_rings: {n_total} POIs, {n_ringed} within {RING_EDGES_MI[-1]} mi "
        f"({', '.join(f'{r}={n}' for r, n in per_ring)})")
    gate("poi_rings_core_nonempty", per_ring and per_ring[0][1] > 0,
         f"core ring POI count = {per_ring[0][1] if per_ring else 0}")


def build_visits_ring_day(con, bronze):
    advan = bronze_path(bronze, "advan_weekly_patterns/*.parquet")
    total_weeks, bad_weeks = con.sql(f"""
        SELECT COUNT(DISTINCT DATE_RANGE_START::DATE),
               COUNT(DISTINCT DATE_RANGE_START::DATE)
                 FILTER (dayofweek(DATE_RANGE_START::DATE) != 1)
        FROM read_parquet('{advan}')
    """).fetchone()
    gate("advan_weeks_monday_aligned", bad_weeks == 0,
         f"{total_weeks} distinct weeks, {bad_weeks} not Monday-aligned")

    con.sql(f"""
        CREATE OR REPLACE TABLE advan_daily AS
        WITH w AS (
            SELECT FOOTPRINT_ID,
                   DATE_RANGE_START::DATE AS ws,
                   VISIT_COUNTS,
                   list_transform(
                       string_split(trim(VISITS_BY_DAY, '[]'), ','),
                       lambda s: TRY_CAST(TRIM(s) AS BIGINT)) AS l
            FROM read_parquet('{advan}'))
        SELECT w.FOOTPRINT_ID, w.ws, w.VISIT_COUNTS,
               list_sum(w.l) AS vbd_sum,
               len(w.l) AS vbd_len,
               w.ws + t.i::INT AS date,
               w.l[t.i::INT + 1] AS visits
        FROM w CROSS JOIN range(7) t(i)
    """)
    # VISITS_BY_DAY vs VISIT_COUNTS differ by rounding-level noise only
    # (verified 2026-07-18: median |diff| 0-1, p99 <= 3). VISITS_BY_DAY is the
    # daily truth; the gate catches structural breaks, not the known noise.
    bad_len, p99 = con.sql("""
        SELECT COUNT(*) FILTER (vbd_len != 7) / 7,
               quantile_cont(ABS(vbd_sum - VISIT_COUNTS), 0.99)
        FROM advan_daily
    """).fetchone()
    gate("advan_vbd_parse", bad_len == 0, f"{bad_len} poi-weeks with array len != 7")
    gate("advan_vbd_tolerance", p99 is not None and p99 <= 5,
         f"p99 |sum(VISITS_BY_DAY) - VISIT_COUNTS| = {p99}")

    con.sql(f"""
        CREATE OR REPLACE TABLE visits_ring_day AS
        SELECT r.ring_id, r.ring, d.date,
               SUM(d.visits) AS visits,
               SUM(d.visits) FILTER (r.location_name IS DISTINCT FROM '{VENUE_POI_NAME}')
                   AS visits_ex_venue,
               SUM(d.visits) FILTER (starts_with(r.naics_code, '{FOOD_NAICS_PREFIX}'))
                   AS visits_food,
               SUM(d.visits) FILTER (r.weeks_present = {total_weeks})
                   AS visits_balanced,
               COUNT(DISTINCT d.FOOTPRINT_ID) AS poi_count
        FROM advan_daily d
        JOIN poi_rings r USING (FOOTPRINT_ID)
        WHERE r.ring_id IS NOT NULL
          AND d.date BETWEEN DATE '{PANEL_START}' AND DATE '{PANEL_END}'
        GROUP BY 1, 2, 3
    """)
    n, dmin, dmax = con.sql(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM visits_ring_day").fetchone()
    log(f"visits_ring_day: {n} ring-days, {dmin} to {dmax}")
    return str(dmax)  # panel end = last day with visit data


def build_calendar_day(con, bronze, panel_end):
    mlb = bronze_path(bronze, "mlb_giants_schedule/mlb_giants_home_games.csv")
    fph = ("MIN(first_pitch_hour)"
           if "first_pitch_hour" in csv_columns(con, mlb) else "NULL")
    park = bronze_path(bronze, "competing_events/oracle_park_events.csv")
    nba = bronze_path(bronze, "competing_events/nba_warriors_schedule.csv")
    wnba = bronze_path(bronze, "competing_events/wnba_valkyries_schedule.csv")
    concerts = bronze_path(bronze, "competing_events/setlistfm_concerts.csv")
    moscone = bronze_path(bronze, "competing_events/moscone_citywide_events.csv")

    street = bronze_path(bronze, STREET_FAIR_REL)
    if can_read(con, street):
        street_sql = f"""(SELECT date::DATE AS date, string_agg(name, '; ') AS street_fair
                          FROM read_csv('{street}') GROUP BY 1)"""
        log("street-fair calendar found in bronze; folding in")
    else:
        street_sql = "(SELECT NULL::DATE AS date, NULL::VARCHAR AS street_fair WHERE false)"
        log(f"street-fair calendar not in bronze yet ({STREET_FAIR_REL}); column stubbed NULL")

    con.sql(f"""
        CREATE OR REPLACE TABLE calendar_day AS
        WITH spine AS (
            SELECT unnest(generate_series(DATE '{PANEL_START}', DATE '{panel_end}',
                                          INTERVAL 1 DAY))::DATE AS date),
        games AS (
            SELECT date::DATE AS date, COUNT(*) AS n_games,
                   SUM(attendance) AS attendance,
                   MAX(CASE WHEN day_night = 'night' THEN 'night' ELSE 'day' END) AS day_night,
                   {fph} AS first_pitch_hour,
                   string_agg(DISTINCT game_type, ',') AS game_types,
                   string_agg(opponent, '; ') AS opponents
            FROM read_csv('{mlb}') GROUP BY 1),
        park_ev AS (
            SELECT date::DATE AS date, string_agg(name, '; ') AS ballpark_event
            FROM read_csv('{park}') GROUP BY 1),
        chase AS (
            SELECT date, string_agg(DISTINCT label, '; ') AS chase_event FROM (
                SELECT date::DATE AS date, name AS label FROM read_csv('{nba}')
                WHERE home_game = 'True' AND venue = 'Chase Center'
                UNION ALL
                SELECT date::DATE, name FROM read_csv('{wnba}')
                WHERE home_game = 'True' AND venue = 'Chase Center'
                UNION ALL
                SELECT date::DATE, artist FROM read_csv('{concerts}')
                WHERE venue = 'Chase Center')
            GROUP BY 1),
        moscone_days AS (
            SELECT unnest(generate_series(start_date::DATE, end_date::DATE,
                                          INTERVAL 1 DAY))::DATE AS date,
                   event, venue
            FROM read_csv('{moscone}')),
        moscone_ev AS (
            SELECT date, string_agg(DISTINCT event, '; ') AS moscone_event
            FROM moscone_days WHERE venue ILIKE '%moscone%' GROUP BY 1),
        citywide_ev AS (
            SELECT date, string_agg(DISTINCT event, '; ') AS citywide_event
            FROM moscone_days WHERE venue NOT ILIKE '%moscone%' GROUP BY 1),
        street_ev AS {street_sql}
        SELECT s.date,
               isodow(s.date) AS dow,
               g.date IS NOT NULL AS giants_home,
               COALESCE(g.n_games, 0) AS n_games,
               g.attendance, g.day_night, g.first_pitch_hour,
               g.game_types, g.opponents,
               p.ballpark_event, c.chase_event, m.moscone_event,
               w.citywide_event, f.street_fair,
               (g.date IS NULL AND p.ballpark_event IS NULL) AS clean_control,
               '2022plus' AS era
        FROM spine s
        LEFT JOIN games g USING (date)
        LEFT JOIN park_ev p USING (date)
        LEFT JOIN chase c USING (date)
        LEFT JOIN moscone_ev m USING (date)
        LEFT JOIN citywide_ev w USING (date)
        LEFT JOIN street_ev f USING (date)
    """)
    n, gh, cc = con.sql("""
        SELECT COUNT(*), COUNT(*) FILTER (giants_home),
               COUNT(*) FILTER (clean_control)
        FROM calendar_day""").fetchone()
    log(f"calendar_day: {n} days, {gh} Giants home days, {cc} clean-control days")
    gate("calendar_has_games", gh > 300, f"{gh} home game days in window")


def build_weather_day(con, bronze):
    noaa = bronze_path(bronze, "noaa_weather/*.csv")
    con.sql(f"""
        CREATE OR REPLACE TABLE weather_day AS
        SELECT DATE::DATE AS date,
               COALESCE(MAX(TMAX) FILTER (STATION = 'USW00023272'),
                        MAX(TMAX) FILTER (STATION = 'USW00023234')) AS tmax,
               COALESCE(MAX(TMIN) FILTER (STATION = 'USW00023272'),
                        MAX(TMIN) FILTER (STATION = 'USW00023234')) AS tmin,
               COALESCE(MAX(TAVG) FILTER (STATION = 'USW00023272'),
                        MAX(TAVG) FILTER (STATION = 'USW00023234')) AS tavg,
               COALESCE(MAX(PRCP) FILTER (STATION = 'USW00023272'),
                        MAX(PRCP) FILTER (STATION = 'USW00023234')) AS prcp,
               COALESCE(MAX(AWND) FILTER (STATION = 'USW00023272'),
                        MAX(AWND) FILTER (STATION = 'USW00023234')) AS awnd
        FROM read_csv('{noaa}', union_by_name = true)
        WHERE DATE::DATE BETWEEN DATE '{PANEL_START}' AND DATE '{PANEL_END}'
        GROUP BY 1
    """)
    log(f"weather_day: {con.sql('SELECT COUNT(*) FROM weather_day').fetchone()[0]} days")


def build_transit_day(con, bronze):
    exits = bronze_path(bronze, "bart_daily_exits/bart_daily_exits.csv")
    od = bronze_path(bronze, "bart_hourly_od/*.parquet")
    sf8 = ", ".join(f"'{s}'" for s in SF8_STATIONS)
    con.sql(f"""
        CREATE OR REPLACE TABLE transit_day AS
        WITH ex AS (
            SELECT date::DATE AS date, em::BIGINT AS bart_em_exits,
                   mt::BIGINT AS bart_mt_exits
            FROM read_csv('{exits}')
            WHERE date::DATE BETWEEN DATE '{PANEL_START}' AND DATE '{PANEL_END}'),
        od AS (
            SELECT date,
                   SUM(trip_count) FILTER (destination IN ('EMBR', 'MONT'))
                       AS od_embr_mont_arrivals,
                   SUM(trip_count) FILTER (destination IN ('EMBR', 'MONT')
                                           AND hour BETWEEN 16 AND 18)
                       AS od_embr_mont_arr_16_18,
                   SUM(trip_count) FILTER (destination IN ({sf8}))
                       AS od_sf8_arrivals
            FROM read_parquet('{od}')
            WHERE date BETWEEN DATE '{PANEL_START}' AND DATE '{PANEL_END}'
            GROUP BY 1)
        SELECT COALESCE(ex.date, od.date) AS date,
               ex.bart_em_exits, ex.bart_mt_exits,
               ex.bart_em_exits + ex.bart_mt_exits AS bart_embr_mont_exits,
               od.od_embr_mont_arrivals, od.od_embr_mont_arr_16_18,
               od.od_sf8_arrivals
        FROM ex FULL OUTER JOIN od USING (date)
    """)
    n, nod = con.sql("SELECT COUNT(*), COUNT(od_sf8_arrivals) FROM transit_day").fetchone()
    log(f"transit_day: {n} days ({nod} with hourly-OD features)")


def build_bikeshare_ring_day(con, bronze):
    """Bay Wheels 2022+ monthly zips -> ring x day trip starts/ends.
    Needs a local bronze (python unzips); on s3 bronze the table is emptied
    and the panel carries NULL bike columns."""
    folder = os.path.join(bronze, "baywheels_bikeshare")
    con.sql("""CREATE OR REPLACE TABLE bikeshare_ring_day (
                   ring_id INT, ring VARCHAR, date DATE,
                   bike_starts BIGINT, bike_ends BIGINT)""")
    if bronze.startswith("s3://") or not os.path.isdir(folder):
        log("bikeshare: bronze is not local; SKIPPING (bike columns will be NULL)")
        QA["bikeshare_built"] = {"ok": True, "detail": "skipped: non-local bronze"}
        return

    end_month = int(PANEL_END[:4] + PANEL_END[5:7])
    zips = sorted(
        f for f in os.listdir(folder)
        if f.endswith(".zip") and re.match(r"^\d{6}", f)
        and 202201 <= int(f[:6]) <= end_month
    )
    log(f"bikeshare: extracting {len(zips)} monthly zips (2022+)")
    with tempfile.TemporaryDirectory(prefix="baywheels_") as tmp:
        for z in zips:
            with zipfile.ZipFile(os.path.join(folder, z)) as zf:
                for m in zf.namelist():
                    if m.endswith(".csv") and "__MACOSX" not in m:
                        base = f"{z[:6]}_{os.path.basename(m)}"
                        with zf.open(m) as src, open(os.path.join(tmp, base), "wb") as dst:
                            dst.write(src.read())
        dist_s = haversine_sql("start_lat", "start_lng")
        dist_e = haversine_sql("end_lat", "end_lng")
        con.sql(f"""
            CREATE OR REPLACE TABLE bike_trips AS
            SELECT TRY_CAST(started_at AS TIMESTAMP)::DATE AS sdate,
                   TRY_CAST(ended_at AS TIMESTAMP)::DATE AS edate,
                   TRY_CAST(start_lat AS DOUBLE) AS start_lat,
                   TRY_CAST(start_lng AS DOUBLE) AS start_lng,
                   TRY_CAST(end_lat AS DOUBLE) AS end_lat,
                   TRY_CAST(end_lng AS DOUBLE) AS end_lng
            FROM read_csv('{tmp}/*.csv', union_by_name = true,
                          all_varchar = true, ignore_errors = true)
        """)
        con.sql(f"""
            CREATE OR REPLACE TABLE bikeshare_ring_day AS
            WITH s AS (
                SELECT {ring_case_sql(f'({dist_s})')} AS ring_id, sdate AS date,
                       COUNT(*) AS bike_starts
                FROM bike_trips
                WHERE start_lat IS NOT NULL
                  AND sdate BETWEEN DATE '{PANEL_START}' AND DATE '{PANEL_END}'
                GROUP BY 1, 2),
            e AS (
                SELECT {ring_case_sql(f'({dist_e})')} AS ring_id, edate AS date,
                       COUNT(*) AS bike_ends
                FROM bike_trips
                WHERE end_lat IS NOT NULL
                  AND edate BETWEEN DATE '{PANEL_START}' AND DATE '{PANEL_END}'
                GROUP BY 1, 2)
            SELECT COALESCE(s.ring_id, e.ring_id) AS ring_id,
                   {ring_label_sql('COALESCE(s.ring_id, e.ring_id)')} AS ring,
                   COALESCE(s.date, e.date) AS date,
                   s.bike_starts, e.bike_ends
            FROM s FULL OUTER JOIN e ON s.ring_id = e.ring_id AND s.date = e.date
            WHERE COALESCE(s.ring_id, e.ring_id) IS NOT NULL
        """)
    n, trips = con.sql("""SELECT COUNT(*), SUM(bike_starts)
                          FROM bikeshare_ring_day""").fetchone()
    log(f"bikeshare_ring_day: {n} ring-days, {trips:,} ringed trip starts")
    QA["bikeshare_built"] = {"ok": True, "detail": f"{n} ring-days from {len(zips)} zips"}


def build_panel(con, panel_end):
    con.sql(f"""
        CREATE OR REPLACE TABLE panel_ring_day AS
        WITH spine AS (
            SELECT c.date, r.ring_id, {ring_label_sql('r.ring_id')} AS ring
            FROM calendar_day c
            CROSS JOIN range(1, {len(RING_LABELS) + 1}) r(ring_id))
        SELECT s.date, s.ring_id, s.ring,
               v.visits, v.visits_ex_venue, v.visits_food, v.visits_balanced,
               v.poi_count,
               b.bike_starts, b.bike_ends,
               c.* EXCLUDE (date),
               w.* EXCLUDE (date),
               t.* EXCLUDE (date)
        FROM spine s
        LEFT JOIN visits_ring_day v ON v.ring_id = s.ring_id AND v.date = s.date
        LEFT JOIN bikeshare_ring_day b ON b.ring_id = s.ring_id AND b.date = s.date
        LEFT JOIN calendar_day c ON c.date = s.date
        LEFT JOIN weather_day w ON w.date = s.date
        LEFT JOIN transit_day t ON t.date = s.date
        ORDER BY s.date, s.ring_id
    """)
    n_days = con.sql("SELECT COUNT(*) FROM calendar_day").fetchone()[0]
    n, dup = con.sql("""
        SELECT COUNT(*), COUNT(*) - COUNT(DISTINCT (date, ring_id))
        FROM panel_ring_day""").fetchone()
    gate("panel_shape", n == n_days * len(RING_LABELS) and dup == 0,
         f"{n} rows vs {n_days} days x {len(RING_LABELS)} rings, {dup} dups")
    miss = con.sql(f"""
        SELECT COUNT(*) FROM panel_ring_day
        WHERE visits IS NULL AND date <= DATE '{panel_end}'""").fetchone()[0]
    gate("panel_visits_coverage", miss == 0,
         f"{miss} ring-days missing visits inside Advan coverage")


def crosscheck_bike_lift(con):
    """Reproduce the validate_join.py Aug-2024 contrast: Bay Wheels trips
    starting <= 1 mi of the park (rings 1-3), game vs non-game days. The
    corrected reference number (officialDate fix, 2026-07-02) is +9.3%."""
    row = con.sql("""
        WITH b AS (
            SELECT date, SUM(bike_starts) AS starts
            FROM bikeshare_ring_day WHERE ring_id <= 3 GROUP BY 1),
        aug AS (
            SELECT b.starts, c.giants_home
            FROM b JOIN calendar_day c USING (date)
            WHERE b.date BETWEEN DATE '2024-08-01' AND DATE '2024-08-31')
        SELECT AVG(starts) FILTER (giants_home),
               AVG(starts) FILTER (NOT giants_home) FROM aug
    """).fetchone()
    if not row or row[0] is None or row[1] is None:
        QA["crosscheck_bike_lift"] = {"ok": True, "detail": "skipped: no bikeshare data"}
        return
    lift = (row[0] - row[1]) / row[1] * 100
    gate("crosscheck_bike_lift", 4 <= lift <= 16,
         f"Aug-2024 game-day lift = {lift:.1f}% (reference ~9.3%)")


def crosscheck_luke_residuals(con, residuals):
    """Report-only: Luke's game_residuals_0_300m `v` vs our ring-1
    visits_food. Established 2026-07-18: his v = daily visits to NAICS-722
    (food services) POIs within 0-300m, and this pipeline reproduces it at
    corr = 1.0000, ratio = 1.0000. Never fails the build (the file may be
    unreachable)."""
    try:
        corr, ratio, n = con.sql(f"""
            SELECT corr(l.v, o.visits_food), AVG(l.v / o.visits_food), COUNT(*)
            FROM '{residuals}' l
            JOIN visits_ring_day o ON o.date = l.date AND o.ring_id = 1
        """).fetchone()
        QA["crosscheck_luke_v"] = {
            "ok": True,
            "detail": f"n={n}, corr={corr:.4f}, mean(his v / our visits)={ratio:.4f}"}
        log(f"cross-check vs Luke's v: n={n}, corr={corr:.4f}, mean ratio={ratio:.4f}")
    except Exception as e:
        QA["crosscheck_luke_v"] = {"ok": True, "detail": f"skipped: {e}"}
        log(f"cross-check vs Luke's v skipped: {e}")


TABLES = ["poi_rings", "visits_ring_day", "calendar_day", "weather_day",
          "transit_day", "bikeshare_ring_day", "panel_ring_day"]


def write_outputs(con, out, bronze, started):
    os.makedirs(out, exist_ok=True)
    counts = {}
    for t in TABLES:
        path = os.path.join(out, f"{t}.parquet")
        con.sql(f"COPY (SELECT * FROM {t}) TO '{path}' (FORMAT parquet)")
        counts[t] = con.sql(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    manifest = {
        "built_at": started.isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "bronze": bronze,
        "params": {"ring_edges_mi": RING_EDGES_MI, "ring_labels": RING_LABELS,
                   "panel_start": PANEL_START, "panel_end": PANEL_END,
                   "food_naics_prefix": FOOD_NAICS_PREFIX},
        "tables": counts,
        "qa": QA,
        "bronze_snapshot": bronze_snapshot(bronze),
    }
    with open(os.path.join(out, "build_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(out, "README.md"), "w") as f:
        f.write(readme_text(manifest))
    log(f"wrote {len(TABLES)} tables + build_manifest.json + README.md to {out}")


def readme_text(m):
    rows = "\n".join(f"| `{t}.parquet` | {n:,} |" for t, n in m["tables"].items())
    qa = "\n".join(f"- {'PASS' if v['ok'] else 'FAIL'} `{k}`: {v['detail']}"
                   for k, v in m["qa"].items())
    return f"""# silver/ - conformed, joined, analysis-grain tables

DERIVED DATA. Built only by `pipeline/build_silver.py` in the team repo
(su26-aai590-Group2); never hand-edited. To change anything here, change the
code or bronze and rebuild. This prefix is synced with `--delete`: it always
reflects exactly one build.

Grain: rings around Oracle Park (37.7786, -122.3893), edges (miles):
{m['params']['ring_edges_mi']} -> rings {m['params']['ring_labels']}.
Window: {m['params']['panel_start']} to {m['params']['panel_end']} (full seasons
2022-2025; 2020-2021 excluded by design; bronze retains raw data beyond the
window, the cutoff is enforced here).

| Table | Rows |
|---|---|
{rows}

`panel_ring_day.parquet` is the analysis deliverable: ring x day with visits
(total / food-services / balanced-POI), Bay Wheels, treatment (Giants games +
attendance + first pitch), confounders (ballpark / Chase / Moscone / citywide /
street-fair events), clean-control flag, weather, and BART features.

## This build

- built_at: {m['built_at']}
- git_sha: {m['git_sha']}
- bronze: {m['bronze']}

QA gates:
{qa}
"""


def main():
    ap = argparse.ArgumentParser(description="Build the silver layer from bronze")
    ap.add_argument("--bronze", default="s3://aai-590-group2-capstone/bronze")
    ap.add_argument("--out", default="./silver")
    ap.add_argument("--residuals",
                    default="s3://aai-590-group2-capstone/eia-nowcast/gold/game_residuals_0_300m.parquet")
    args = ap.parse_args()

    started = datetime.datetime.now()
    con = duckdb.connect()
    if "s3://" in args.bronze + args.residuals:
        con.sql("CREATE OR REPLACE SECRET aws (TYPE s3, PROVIDER credential_chain, "
                "REGION 'us-east-2')")

    log(f"bronze={args.bronze}  out={args.out}")
    build_poi_rings(con, args.bronze)
    panel_end = build_visits_ring_day(con, args.bronze)
    build_calendar_day(con, args.bronze, panel_end)
    build_weather_day(con, args.bronze)
    build_transit_day(con, args.bronze)
    build_bikeshare_ring_day(con, args.bronze)
    build_panel(con, panel_end)
    crosscheck_bike_lift(con)
    crosscheck_luke_residuals(con, args.residuals)
    write_outputs(con, args.out, args.bronze, started)
    log(f"done in {(datetime.datetime.now() - started).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
