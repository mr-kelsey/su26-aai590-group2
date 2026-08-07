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

# Ring edges in METERS (team decision 2026-07-18: single metric unit, matching
# the sources; Advan's only native distance field, DISTANCE_FROM_HOME, is in
# meters). Change HERE only. Note ring 1 (0-250m) is tighter than Luke's
# original 0-300m ring, so his eia-nowcast series is a near-match, not exact.
RING_EDGES_M = [0, 250, 500, 1000, 2500, 5000]
RING_LABELS = ["0-250m", "250-500m", "500m-1km", "1-2.5km", "2.5-5km"]

PANEL_START = "2022-01-01"  # study window: 2022+ era (Advan starts 2020-01-06)
PANEL_END = "2025-12-31"    # study window cutoff: full seasons 2022-2025 only

# The treatment is a REAL Giants home game. game_type 'S' is spring training:
# four Bay Bridge Series exhibitions fall inside the window (2023-03-27,
# 2024-03-26, 2025-03-24, 2025-03-25). They draw 20-30k against a 35k regular
# median, and being late-March they draw control sets that are 63% deep
# offseason against 8.8% across all matched pairs, so they were the worst-matched
# treatment days in the panel. Postseason ('D', 'L', 'W') and the 'E' exhibition
# code against non-MLB opposition are handled explicitly: postseason stays,
# exhibitions do not.
EXHIBITION_GAME_TYPES = ("S", "E")
TREATMENT_GAME_FILTER = (
    "game_type NOT IN ("
    + ", ".join(f"'{g}'" for g in EXHIBITION_GAME_TYPES)
    + ")"
)


def window_week_filter(start: str, end: str) -> str:
    """SQL predicate keeping Advan weeks that overlap the study window.

    `visits_balanced` counts a POI as balanced when it reports in EVERY week, so
    the week set it is measured against has to be the study window's, not the
    whole extract's. The extract spans 2020-01-06 to 2026-05-25, which includes
    the COVID era the project excludes by design and a 2026 tail past PANEL_END;
    requiring presence across all of that disqualified about 2,400 POIs that are
    in fact present in every week we actually model.
    """
    return (f"DATE_RANGE_START::DATE >= DATE '{start}' "
            f"AND DATE_RANGE_START::DATE <= DATE '{end}'")

# Occupancy (visitor-hours) window is NARROWER than the visits panel. Advan
# changed how VISITS_BY_EACH_HOUR is constructed between 2022 Q4 and 2023 Q1:
# on a fixed set of 4,284 POIs (hourly present all four years), the
# hourly-sum / daily-sum ratio jumps 1.64 -> 2.77 (+69%) at the boundary, then
# drifts smoothly (~2.07 by 2025). 2022 visitor-hours are therefore not
# comparable to 2023+ and are excluded. VISITS_BY_DAY shows no such break; the
# daily panel keeps the full window. Gate advan_hourly_construction_stable
# enforces both facts. Evidence: docs/design/2026-07-28-advan-occupancy.md.
OCC_START = "2023-01-02"    # first MONDAY-ALIGNED Advan week of the new
                            # construction; the week of 2022-12-26 straddles
                            # the break and would leak one old-era day (Jan 1,
                            # an offseason Sunday) into the panel
OCC_END = "2025-12-31"      # occupancy window cutoff (= PANEL_END)
                            # (decided 2026-07-18). Bronze retains later raw
                            # data; the scope is enforced HERE, never by
                            # trimming raw files.
FOOD_NAICS_PREFIX = "722"   # Food Services and Drinking Places
VENUE_POI_NAME = "Oracle Park"  # the stadium's own Advan POI (excluded in *_ex_venue)
SF8_STATIONS = ("EMBR", "MONT", "POWL", "CIVC", "16TH", "24TH", "GLEN", "BALB")

# Pre-wired confounder drop-in: when Luke's street-fair calendar lands in
# bronze under this name (columns: date,name), the next rebuild picks it up
# with no code change.
# Luke's hand-verified fair list (adopted verbatim from his eia-nowcast import,
# commit eb060ba, schemas/street_fairs_near_oracle.csv). Schema:
# event_name,date,lat,lon,dist_km,status; only status='happened' rows count.
STREET_FAIR_REL = "competing_events/street_fairs_near_oracle.csv"

QA = {}  # gate name -> result, written into build_manifest.json


# ------------------------------------------------------------------- helpers

def log(msg):
    print(f"[build_silver] {msg}", flush=True)


# --------------------------------------------------------------- QA vocabulary
#
# Kept deliberately identical to build_gold.py's block rather than factored into
# a shared module: both scripts are standalone by design (run by path, "duckdb
# only", copy-one-file deployable) and already duplicate log/gate/git_sha/
# write_outputs/readme_text for the same reason.
#
# Every silver check is an INTEGRITY gate and stays hard: parse, array length,
# Monday alignment, panel shape, unit conversions, coverage floors, and the
# local-vs-UTC hour reading. Silver makes no claim about what the data shows, so
# there is nothing here to soften. The kinds below exist because "PASS" used to
# be printed for four different things, including a swallowed exception.
#
#   gate         integrity/shape/units/coverage. HARD: aborts the build.
#   crosscheck   agreement with an external artifact. Soft.
#   skipped      the check did not run, and why. Never counts as agreement.
#   note         provenance only, no verdict.

def gate(name, ok, detail):
    """Integrity gate. Aborts the build on failure."""
    QA[name] = {"kind": "gate", "ok": bool(ok), "detail": detail}
    log(f"GATE {'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        sys.exit(f"QA gate failed: {name}: {detail}")


def crosscheck(name, ok, detail):
    """Agreement with an artifact this build does not own. Never aborts."""
    QA[name] = {"kind": "crosscheck", "ok": bool(ok), "detail": detail}
    log(f"CROSSCHECK {'AGREES' if ok else 'DISAGREES'} {name}: {detail}")


def skipped(name, why):
    """The check did not run. Recorded ok=False so it can never read as a pass."""
    QA[name] = {"kind": "skipped", "ok": False, "detail": f"did not run: {why}"}
    log(f"SKIPPED {name}: {why}")


def note(name, detail):
    """Provenance only: no pass/fail verdict is implied."""
    QA[name] = {"kind": "note", "detail": detail}
    log(f"NOTE {name}: {detail}")


_VERDICT = {
    ("gate", True): "PASS", ("gate", False): "FAIL",
    ("crosscheck", True): "AGREES", ("crosscheck", False): "DISAGREES",
    ("skipped", False): "DID NOT RUN", ("skipped", True): "DID NOT RUN",
}

_QA_SECTIONS = [
    ("gate", "Integrity gates (a failure aborts the build)"),
    ("crosscheck", "External crosschecks"),
    ("skipped", "Checks that did not run"),
    ("note", "Notes (no verdict)"),
]


def qa_markdown(qa):
    """Render the QA log by kind, so each entry states what it actually means."""
    out = []
    for kind, heading in _QA_SECTIONS:
        rows = [(k, v) for k, v in qa.items() if v.get("kind", "gate") == kind]
        if not rows:
            continue
        out.append(f"**{heading}**\n")
        for k, v in rows:
            verdict = _VERDICT.get((kind, bool(v.get("ok"))))
            detail = v["detail"]
            if not isinstance(detail, str):
                detail = json.dumps(detail, default=str)
            out.append(f"- {verdict + ' ' if verdict else ''}`{k}`: {detail}")
        out.append("")
    for kind, one, many, tail in (
        ("skipped", "check DID NOT RUN", "checks DID NOT RUN",
         "That coverage is UNVERIFIED in this build."),
        ("crosscheck", "crosscheck DISAGREES", "crosschecks DISAGREE",
         "Reconcile before quoting."),
    ):
        bad = [k for k, v in qa.items()
               if v.get("kind") == kind and not v.get("ok")]
        if bad:
            out.append(f"> {len(bad)} {one if len(bad) == 1 else many}: "
                       f"{', '.join(f'`{k}`' for k in bad)}. {tail}")
            out.append("")
    return "\n".join(out).rstrip()


def haversine_sql(lat_col, lon_col):
    # meters (mean Earth radius 6,371,008.8 m)
    lat0, lon0 = ORACLE_PARK
    return (
        f"2 * 6371008.8 * asin(sqrt("
        f"sin(radians({lat_col} - ({lat0})) / 2) ^ 2 + "
        f"cos(radians({lat0})) * cos(radians({lat_col})) * "
        f"sin(radians({lon_col} - ({lon0})) / 2) ^ 2))"
    )


def ring_case_sql(dist_col):
    parts = []
    for i, label in enumerate(RING_LABELS):
        parts.append(
            f"WHEN {dist_col} < {RING_EDGES_M[i + 1]} THEN {i + 1}"
        )
    return f"CASE WHEN {dist_col} < {RING_EDGES_M[0]} THEN NULL " + " ".join(parts) + " ELSE NULL END"


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
                   COUNT(DISTINCT DATE_RANGE_START::DATE)
                       FILTER ({window_week_filter(PANEL_START, PANEL_END)})
                       AS weeks_present,
                   MIN(DATE_RANGE_START::DATE) AS first_week,
                   MAX(DATE_RANGE_START::DATE) AS last_week
            FROM read_parquet('{advan}')
            GROUP BY FOOTPRINT_ID),
        d AS (SELECT *, {dist} AS dist_m FROM pois)
        SELECT * EXCLUDE (dist_m),
               ROUND(dist_m, 1) AS dist_m,
               {ring_case_sql('dist_m')} AS ring_id,
               {ring_label_sql(ring_case_sql('dist_m'))} AS ring
        FROM d
    """)
    n_total, n_ringed = con.sql(
        "SELECT COUNT(*), COUNT(ring_id) FROM poi_rings").fetchone()
    per_ring = con.sql(
        "SELECT ring, COUNT(*) FROM poi_rings WHERE ring IS NOT NULL "
        "GROUP BY ring ORDER BY min(ring_id)").fetchall()
    log(f"poi_rings: {n_total} POIs, {n_ringed} within {RING_EDGES_M[-1]} m "
        f"({', '.join(f'{r}={n}' for r, n in per_ring)})")
    gate("poi_rings_core_nonempty", per_ring and per_ring[0][1] > 0,
         f"core ring POI count = {per_ring[0][1] if per_ring else 0}")


def build_visits_ring_day(con, bronze):
    advan = bronze_path(bronze, "advan_weekly_patterns/*.parquet")
    in_window = window_week_filter(PANEL_START, PANEL_END)
    total_weeks, bad_weeks = con.sql(f"""
        SELECT COUNT(DISTINCT DATE_RANGE_START::DATE),
               COUNT(DISTINCT DATE_RANGE_START::DATE)
                 FILTER (dayofweek(DATE_RANGE_START::DATE) != 1)
        FROM read_parquet('{advan}')
        WHERE {in_window}
    """).fetchone()
    gate("advan_weeks_monday_aligned", bad_weeks == 0,
         f"{total_weeks} distinct weeks in window, {bad_weeks} not Monday-aligned")

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


def build_occupancy(con, bronze):
    """Hourly VISITOR-HOURS: sparse POI grain, coverage map, dense ring panel.

    VISITS_BY_EACH_HOUR is NOT a headcount spread across the day. Advan dedupes
    unique visitors WITHIN each bucket, and the bucket size differs by column:
    week for VISITOR_COUNTS, day for VISITS_BY_DAY / VISIT_COUNTS ("the sum of
    each day's unique visitors", per the v2.8 spec), hour here. A visitor
    spanning four hourly buckets counts in all four, so summing the array over
    a day yields visitor-hours, which exceeds VISITS_BY_DAY by a dwell-dependent
    factor (about 4x at the ballpark, 0.86x for sub-30-minute POIs).

    We NEVER impute the ~38% of POI-weeks where the array is null. Filling them
    from VISITS_BY_DAY would mix visits into a visitor-hours column. Coverage is
    published instead, and it is not random: hourly is null for 74.9% of POIs in
    the 10-99 visit tier vs 11.0% of the 1000+ tier.

    Hour index i maps to local date week_start + i//24 at local hour i%24. The
    parquet stamps DATE_RANGE_START as timestamp[ms, tz=UTC] with a naive
    midnight value; that UTC label is a Dewey delivery artifact. Advan's spec
    says local Monday midnight with a local GMT offset. Reading it as local is
    deliberate, and gate advan_vbh_local_time defends it (a UTC reading would
    put a 13:00 Sunday matinee at 06:00).

    Full detail: docs/design/2026-07-28-advan-occupancy.md
    """
    advan = bronze_path(bronze, "advan_weekly_patterns/*.parquet")

    # Restrict to ringed POIs and window-overlapping weeks BEFORE the 168x
    # explode; the unrestricted cross join is ~950M rows.
    con.sql(f"""
        CREATE OR REPLACE TABLE occ_weeks AS
        SELECT a.FOOTPRINT_ID,
               a.DATE_RANGE_START::DATE AS week_start,
               a.VISITS_BY_EACH_HOUR IS NOT NULL AS has_hourly,
               list_transform(
                   string_split(trim(a.VISITS_BY_EACH_HOUR, '[]'), ','),
                   lambda s: TRY_CAST(TRIM(s) AS BIGINT)) AS h,
               a.VISIT_COUNTS::BIGINT AS visits,
               list_sum(list_transform(
                   string_split(trim(a.VISITS_BY_DAY, '[]'), ','),
                   lambda s: TRY_CAST(TRIM(s) AS BIGINT))) AS vd_sum,
               a.BUCKETED_DWELL_TIMES AS dwell,
               a.OPEN_DATE::DATE  AS open_date,
               a.CLOSE_DATE::DATE AS close_date
        FROM read_parquet('{advan}') a
        JOIN poi_rings r USING (FOOTPRINT_ID)
        WHERE r.ring_id IS NOT NULL
          -- weeks FULLY inside the window only: a straddling week (e.g.
          -- 2022-12-26) carries the pre-break construction
          AND a.DATE_RANGE_START::DATE >= DATE '{OCC_START}'
          AND a.DATE_RANGE_START::DATE <= DATE '{OCC_END}'
    """)
    n_weeks, bad_len = con.sql("""
        SELECT COUNT(DISTINCT week_start),
               COUNT(*) FILTER (has_hourly AND len(h) != 168)
        FROM occ_weeks""").fetchone()
    gate("advan_vbh_parse", bad_len == 0,
         f"{bad_len} POI-weeks with hourly array len != 168 ({n_weeks} weeks)")

    # Advan changed the hourly construction at 2023 Q1: on a fixed POI set the
    # hourly/daily ratio jumps +69% (1.64 -> 2.77) at the boundary. Two checks:
    # (a) the window starts 2023+, so the break stays excluded; (b) WITHIN the
    # window, the same fixed-set ratio drifts by at most 15% quarter over
    # quarter, so a future vendor re-break fails the build instead of silently
    # shifting every cross-season comparison. Fixed set = POIs with hourly in
    # every window year, so composition cannot fake stability.
    assert OCC_START >= "2023-01-02", (
        "occupancy window must start at/after the first Monday-aligned 2023 "
        "week: Advan hourly construction break at 2023 Q1 (+69% on fixed "
        "POIs), and the 2022-12-26 week straddles it")
    max_qoq = con.sql("""
        WITH fixed AS (
            SELECT FOOTPRINT_ID FROM occ_weeks WHERE has_hourly
            GROUP BY 1
            HAVING COUNT(DISTINCT year(week_start)) =
                   (SELECT COUNT(DISTINCT year(week_start)) FROM occ_weeks)),
        q AS (
            SELECT year(week_start) AS yr, quarter(week_start) AS qt,
                   SUM(list_sum(h)) * 1.0 / SUM(vd_sum) AS ratio
            FROM occ_weeks JOIN fixed USING (FOOTPRINT_ID)
            WHERE has_hourly AND vd_sum > 0
            GROUP BY 1, 2),
        d AS (
            SELECT ratio / lag(ratio) OVER (ORDER BY yr, qt) - 1 AS chg FROM q)
        SELECT ROUND(MAX(ABS(chg)), 4) FROM d
    """).fetchone()[0]
    gate("advan_hourly_construction_stable",
         max_qoq is not None and max_qoq <= 0.15,
         f"max quarter-over-quarter fixed-POI hourly/daily ratio change = "
         f"{max_qoq:.1%} within {OCC_START}..{OCC_END}, want <= 15% "
         f"(the excluded 2022->2023 break was +69%)")

    con.sql("""
        CREATE OR REPLACE TABLE occupancy_poi_week_coverage AS
        SELECT FOOTPRINT_ID AS footprint_id, week_start, has_hourly
        FROM occ_weeks
    """)

    # Balanced set: hourly in >= 95% of window weeks AND open the whole
    # window (OPEN_DATE sentinel 1970-01-01 = "before 2010"; CLOSE_DATE
    # 2038-01-01 = "still open"). The strict all-weeks criterion left only 5
    # core-ring POIs (measured 2026-07-28: 2,246 total); the design doc's
    # documented fallback (>= 95%) ships instead (3,986 total). Ring 1 stays
    # thin either way (6 relaxed) because only ~21 of its 37 POIs report
    # hourly at all: treat ring-1 visitor_hours_balanced as a weak column.
    bal_floor = -(-95 * n_weeks // 100)  # ceil(0.95 * n_weeks)
    con.sql(f"""
        CREATE OR REPLACE TABLE occ_balanced AS
        SELECT FOOTPRINT_ID
        FROM occ_weeks
        GROUP BY FOOTPRINT_ID
        HAVING COUNT(*) FILTER (has_hourly) >= {bal_floor}
           AND MIN(open_date)  <= DATE '{OCC_START}'
           AND MAX(close_date) >= DATE '{OCC_END}'
    """)

    # Sparse: non-zero hours only. 75.65% of POI-hours are 0, which cuts the
    # table from ~268M rows to ~72M. Absence is disambiguated by the coverage
    # table: missing + has_hourly => true zero, missing + NOT has_hourly =>
    # unknown.
    con.sql(f"""
        CREATE OR REPLACE TABLE occupancy_poi_hour AS
        SELECT w.FOOTPRINT_ID AS footprint_id,
               (w.week_start + (t.i // 24)::INT)::DATE AS date,
               (t.i % 24)::SMALLINT AS hour,
               w.h[t.i + 1]::BIGINT AS visitor_hours,
               r.ring_id, r.naics_code
        FROM occ_weeks w
        CROSS JOIN range(168) t(i)
        JOIN poi_rings r ON r.FOOTPRINT_ID = w.FOOTPRINT_ID
        WHERE w.has_hourly
          AND w.h[t.i + 1] > 0
          AND (w.week_start + (t.i // 24)::INT)::DATE
              BETWEEN DATE '{OCC_START}' AND DATE '{OCC_END}'
    """)
    n_poi_hour = con.sql("SELECT COUNT(*) FROM occupancy_poi_hour").fetchone()[0]
    log(f"occupancy_poi_hour: {n_poi_hour:,} non-zero POI-hours")
    # Equality join only: weeks are Monday-aligned (gated upstream), so the
    # containing week is date - (isodow-1). A BETWEEN range predicate here
    # makes DuckDB hash-join every POI-hour against every coverage week per
    # POI and post-filter (billions of pairs, effectively hangs).
    bad_sparse = con.sql("""
        SELECT COUNT(*) FROM occupancy_poi_hour o
        LEFT JOIN occupancy_poi_week_coverage c
          ON c.footprint_id = o.footprint_id
         AND c.week_start = o.date - (isodow(o.date) - 1)::INT
        WHERE o.visitor_hours <= 0
           OR c.has_hourly IS DISTINCT FROM TRUE
    """).fetchone()[0]
    gate("occupancy_sparse_integrity", bad_sparse == 0,
         f"{bad_sparse} rows non-positive or outside a covered POI-week")

    # Dense ring x date x hour spine, so the panel is shape-checkable and a
    # true zero hour is an explicit 0 rather than a missing row.
    con.sql(f"""
        CREATE OR REPLACE TABLE occupancy_ring_hour AS
        WITH spine AS (
            SELECT r.ring_id, r.ring, d.date, h.hour::SMALLINT AS hour
            FROM (SELECT DISTINCT ring_id, ring FROM poi_rings
                  WHERE ring_id IS NOT NULL) r
            CROSS JOIN (SELECT UNNEST(generate_series(
                DATE '{OCC_START}', DATE '{OCC_END}', INTERVAL 1 DAY))::DATE
                AS date) d
            CROSS JOIN (SELECT UNNEST(generate_series(0, 23)) AS hour) h),
        agg AS (
            SELECT o.ring_id, o.date, o.hour,
                   SUM(o.visitor_hours) AS visitor_hours,
                   SUM(o.visitor_hours) FILTER (
                       p.location_name IS DISTINCT FROM '{VENUE_POI_NAME}')
                       AS visitor_hours_ex_venue,
                   SUM(o.visitor_hours) FILTER (
                       starts_with(o.naics_code, '{FOOD_NAICS_PREFIX}'))
                       AS visitor_hours_food,
                   SUM(o.visitor_hours) FILTER (b.FOOTPRINT_ID IS NOT NULL)
                       AS visitor_hours_balanced
            FROM occupancy_poi_hour o
            JOIN poi_rings p ON p.FOOTPRINT_ID = o.footprint_id
            LEFT JOIN occ_balanced b ON b.FOOTPRINT_ID = o.footprint_id
            GROUP BY 1, 2, 3),
        -- coverage is a POI-WEEK property, so it does not vary within a date;
        -- it is repeated on each of the 24 hourly rows for convenience.
        cov AS (
            SELECT p.ring_id, d.date,
                   COUNT(*) FILTER (c.has_hourly) AS poi_covered,
                   COUNT(*) AS poi_total,
                   SUM(w.visits) FILTER (c.has_hourly)
                     / NULLIF(SUM(w.visits), 0) AS visit_share_covered
            FROM occupancy_poi_week_coverage c
            JOIN poi_rings p ON p.FOOTPRINT_ID = c.footprint_id
            JOIN occ_weeks w ON w.FOOTPRINT_ID = c.footprint_id
                            AND w.week_start = c.week_start
            CROSS JOIN (SELECT UNNEST(generate_series(0, 6)) AS off) o6
            JOIN (SELECT UNNEST(generate_series(
                    DATE '{OCC_START}', DATE '{OCC_END}',
                    INTERVAL 1 DAY))::DATE AS date) d
                 ON d.date = c.week_start + o6.off::INT
            GROUP BY 1, 2)
        SELECT s.ring_id, s.ring, s.date, s.hour,
               COALESCE(a.visitor_hours, 0)::BIGINT          AS visitor_hours,
               COALESCE(a.visitor_hours_ex_venue, 0)::BIGINT AS visitor_hours_ex_venue,
               COALESCE(a.visitor_hours_food, 0)::BIGINT     AS visitor_hours_food,
               COALESCE(a.visitor_hours_balanced, 0)::BIGINT AS visitor_hours_balanced,
               COALESCE(c.poi_covered, 0)                AS poi_covered,
               COALESCE(c.poi_total, 0)                  AS poi_total,
               c.visit_share_covered
        FROM spine s
        LEFT JOIN agg a USING (ring_id, date, hour)
        LEFT JOIN cov c USING (ring_id, date)
    """)
    n_rh, n_days, n_rings, dups = con.sql("""
        SELECT COUNT(*), COUNT(DISTINCT date), COUNT(DISTINCT ring_id),
               COUNT(*) - COUNT(DISTINCT (ring_id, date, hour))
        FROM occupancy_ring_hour""").fetchone()
    log(f"occupancy_ring_hour: {n_rh:,} rows ({n_days} days x 24h x {n_rings} rings)")
    gate("occupancy_panel_shape", n_rh == n_days * 24 * n_rings and dups == 0,
         f"{n_rh} rows vs {n_days} days x 24 x {n_rings} rings, {dups} dups")

    n_bal = con.sql("SELECT COUNT(*) FROM occ_balanced").fetchone()[0]
    bal_core = con.sql("""
        SELECT COUNT(*) FROM occ_balanced b
        JOIN poi_rings p USING (FOOTPRINT_ID) WHERE p.ring_id = 1""").fetchone()[0]
    log(f"occ_balanced: {n_bal} POIs with hourly in >= {bal_floor}/{n_weeks} "
        f"weeks and open all window ({bal_core} in the core ring; ring-1 "
        f"balanced is structurally thin, see README)")

    worst = con.sql("""
        SELECT ring, ROUND(median(visit_share_covered), 3) AS m
        FROM occupancy_ring_hour GROUP BY ring ORDER BY m LIMIT 1""").fetchone()
    gate("occupancy_coverage_floor", worst is not None and worst[1] >= 0.80,
         f"lowest ring median visit_share_covered = {worst[1]} in {worst[0]}")


# US federal holidays 2022-2026, ACTUAL dates rather than observed-Monday
# shifts: foot traffic responds to the day itself (July 4 is July 4 even when
# offices observe the 3rd). Static list, no data source needed.
US_FED_HOLIDAYS = [
    "2022-01-01", "2022-01-17", "2022-02-21", "2022-05-30", "2022-06-19",
    "2022-07-04", "2022-09-05", "2022-10-10", "2022-11-11", "2022-11-24",
    "2022-12-25",
    "2023-01-01", "2023-01-16", "2023-02-20", "2023-05-29", "2023-06-19",
    "2023-07-04", "2023-09-04", "2023-10-09", "2023-11-11", "2023-11-23",
    "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-05-27", "2024-06-19",
    "2024-07-04", "2024-09-02", "2024-10-14", "2024-11-11", "2024-11-28",
    "2024-12-25",
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27",
    "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19",
    "2026-07-04", "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26",
    "2026-12-25",
]


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
        street_sql = f"""(SELECT date::DATE AS date,
                                 string_agg(event_name, '; ') AS street_fair
                          FROM read_csv('{street}')
                          WHERE status = 'happened' GROUP BY 1)"""
        log("street-fair calendar found in bronze; folding in (status='happened')")
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
            FROM read_csv('{mlb}')
            WHERE {TREATMENT_GAME_FILTER} GROUP BY 1),
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
               s.date IN ({", ".join(f"DATE '{d}'" for d in US_FED_HOLIDAYS)})
                   AS us_federal_holiday,
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


def build_weather_hour(con, bronze):
    """HOURLY weather covariates from NOAA LCD v2, SFO only (USW00023234).

    Downtown SF (USW00023272) has ZERO hourly temperature rows in LCD
    (verified 2026-07-28: SOD/SOM daily summaries only), so there is no
    downtown-first option at hour grain; the daily panel keeps it for tmax.

    LCD gotchas handled here, documented in the bronze folder README:
    - DATE is LOCAL STANDARD TIME year-round (UTC-8, never PDT). Advan hours
      are wall clock, so LST is shifted to America/Los_Angeles wall time.
      Fall-back day: two obs map into wall-clock hour 1 (averaged); spring-
      forward day: hour 2 has no obs. Two quirk hours per year, tolerated by
      the coverage gate.
    - Keep FM-15 (routine METAR) rows only.
    - 'T' = trace precip -> 0.0; '*'/'s' QC suffixes stripped before casting.
    - UNITS: LCD v2 access files are the METRIC edition (deg C / mm / m/s),
      verified 2026-07-28 (Jan SF hourly temps ~10, i.e. C not F), while
      weather_day (GHCN standard) is deg F / inches / mph. Converted here to
      F / inches / mph so every silver weather column shares one convention.
      The weather_hour_vs_daily gate caught the original mismatch (p95 diff
      51.6 F-vs-C) and guards the conversion.
    """
    lcd = bronze_path(bronze, "noaa_weather_hourly/LCD_*.csv")
    con.sql(f"""
        CREATE OR REPLACE TABLE weather_hour AS
        WITH obs AS (
            SELECT timezone('America/Los_Angeles',
                       timezone('UTC', DATE::TIMESTAMP + INTERVAL 8 HOUR))
                       AS local_ts,
                   TRY_CAST(regexp_replace(HourlyDryBulbTemperature,
                       '[^-0-9.]', '', 'g') AS DOUBLE) AS temp,
                   CASE WHEN trim(HourlyPrecipitation) = 'T' THEN 0.0
                        ELSE TRY_CAST(regexp_replace(HourlyPrecipitation,
                            '[^0-9.]', '', 'g') AS DOUBLE) END AS prcp,
                   TRY_CAST(regexp_replace(HourlyWindSpeed,
                       '[^0-9.]', '', 'g') AS DOUBLE) AS wind
            FROM read_csv('{lcd}', union_by_name=true, all_varchar=true)
            WHERE trim(REPORT_TYPE) = 'FM-15')
        SELECT local_ts::DATE AS date,
               hour(local_ts)::SMALLINT AS hour,
               ROUND(AVG(temp) * 9 / 5 + 32, 1) AS temp_hr,   -- C -> F
               ROUND(MAX(prcp) / 25.4, 3) AS prcp_hr,         -- mm -> inches
               ROUND(AVG(wind) * 2.23694, 1) AS wind_hr,      -- m/s -> mph
               'USW00023234' AS station_used
        FROM obs
        WHERE local_ts::DATE BETWEEN DATE '{OCC_START}' AND DATE '{OCC_END}'
        GROUP BY 1, 2
    """)
    n_hours, covered = con.sql(f"""
        SELECT (DATE '{OCC_END}' - DATE '{OCC_START}' + 1) * 24,
               COUNT(*) FILTER (temp_hr IS NOT NULL)
        FROM weather_hour""").fetchone()
    share = covered / n_hours
    gate("weather_hour_coverage", share >= 0.95,
         f"{covered:,}/{n_hours:,} window hours with hourly temp ({share:.1%})")
    # Same-station check (SFO hourly vs SFO daily TMAX): weather_day.tmax is
    # downtown-first, and SF microclimates run ~5-10 F apart on warm days,
    # which would drown the unit signal. Hourly max under-samples a continuous
    # daily max slightly, so a small tolerance remains.
    daily = bronze_path(bronze, "noaa_weather/*.csv")
    p95 = con.sql(f"""
        SELECT quantile_cont(ABS(h.mx - d.tmax), 0.95)
        FROM (SELECT date, MAX(temp_hr) AS mx FROM weather_hour GROUP BY 1) h
        JOIN (SELECT DATE::DATE AS date, MAX(TMAX) AS tmax
              FROM read_csv('{daily}', union_by_name = true)
              WHERE STATION = 'USW00023234' GROUP BY 1) d USING (date)
        WHERE d.tmax IS NOT NULL AND h.mx IS NOT NULL
    """).fetchone()[0]
    gate("weather_hour_vs_daily", p95 is not None and p95 <= 4.0,
         f"p95 |daily max of hourly temp - SFO daily TMAX| = {p95:.1f} F "
         f"(same station; catches unit mismatches, e.g. the LCD metric "
         f"edition read as F scores ~50)")
    log(f"weather_hour: {con.sql('SELECT COUNT(*) FROM weather_hour').fetchone()[0]:,} hours")


# Chase Center event windows (hours around tip-off / show start)
EVENT_PRE_H = 2       # crowd builds from ~2h before tip-off
EVENT_POST_H = 3      # ~game length + egress after tip-off
CONCERT_DEFAULT_HOURS = (19, 23)  # setlist.fm has no times; documented default


def build_event_hour(con, bronze):
    """HOUR-grain Chase Center event windows for the occupancy covariates.

    NBA/WNBA home games use the real tip-off hour (start_hour_local from the
    2026-07-28 ESPN re-pull, which also fixed the UTC +1-day date bug on 904
    NBA + 63 WNBA rows): flagged window = tip - EVENT_PRE_H .. tip +
    EVENT_POST_H. Chase concerts have no start times in setlist.fm, so they
    get the documented default {CONCERT_DEFAULT_HOURS} window. Moscone and
    citywide events stay day-grain in calendar_day (multi-day conventions).
    Sparse table: only flagged (date, hour) rows exist.
    """
    nba = bronze_path(bronze, "competing_events/nba_warriors_schedule.csv")
    wnba = bronze_path(bronze, "competing_events/wnba_valkyries_schedule.csv")
    concerts = bronze_path(bronze, "competing_events/setlistfm_concerts.csv")
    con.sql(f"""
        CREATE OR REPLACE TABLE event_hour AS
        WITH ev AS (
            SELECT date::DATE AS date, name AS detail, 'nba' AS kind,
                   TRY_CAST(start_hour_local AS INT) AS tip
            FROM read_csv('{nba}')
            WHERE home_game = 'True' AND venue = 'Chase Center'
            UNION ALL
            SELECT date::DATE, name, 'wnba', TRY_CAST(start_hour_local AS INT)
            FROM read_csv('{wnba}')
            WHERE home_game = 'True' AND venue = 'Chase Center'
            UNION ALL
            SELECT date::DATE, artist, 'concert_default', NULL
            FROM read_csv('{concerts}')
            WHERE venue = 'Chase Center'),
        windows AS (
            SELECT date, detail, kind,
                   CASE WHEN tip IS NOT NULL THEN GREATEST(tip - {EVENT_PRE_H}, 0)
                        ELSE {CONCERT_DEFAULT_HOURS[0]} END AS h_lo,
                   CASE WHEN tip IS NOT NULL THEN LEAST(tip + {EVENT_POST_H}, 23)
                        ELSE {CONCERT_DEFAULT_HOURS[1]} END AS h_hi
            FROM ev
            WHERE date BETWEEN DATE '{OCC_START}' AND DATE '{OCC_END}')
        SELECT w.date, h.hour::SMALLINT AS hour,
               TRUE AS chase_event_hour,
               string_agg(DISTINCT w.kind || ': ' || w.detail, '; ') AS detail
        FROM windows w
        JOIN (SELECT UNNEST(generate_series(0, 23)) AS hour) h
          ON h.hour BETWEEN w.h_lo AND w.h_hi
        GROUP BY 1, 2
    """)
    n, days = con.sql(
        "SELECT COUNT(*), COUNT(DISTINCT date) FROM event_hour").fetchone()
    orphan = con.sql("""
        SELECT COUNT(DISTINCT e.date) FROM event_hour e
        LEFT JOIN calendar_day c USING (date)
        WHERE c.chase_event IS NULL
    """).fetchone()[0]
    gate("event_hour_consistency", orphan == 0,
         f"{orphan} event_hour dates not flagged chase_event in calendar_day "
         f"({n} flagged hours over {days} days)")
    log(f"event_hour: {n:,} flagged hours across {days} Chase event days")


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
        skipped("bikeshare_built", "non-local bronze; bike columns are NULL")
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
    note("bikeshare_built", f"{n} ring-days from {len(zips)} zips")


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


def crosscheck_occupancy(con):
    """Three independent checks that the visitor-hours reading is right.

    Runs after calendar_day because two of them need game timing. Kept separate
    from build_occupancy so a calendar change cannot silently disable them.
    """
    # 1. LOCAL TIME. If the hour index were UTC, a 13:00 Sunday matinee would
    # land at 06:00. Peak can legitimately trail first pitch by an hour or two
    # (attendance builds through the early innings), hence the +/-3 tolerance.
    n_games, aligned = con.sql("""
        WITH peaks AS (
            SELECT o.date, c.first_pitch_hour,
                   arg_max(o.hour, o.visitor_hours) AS peak_hour
            FROM occupancy_ring_hour o
            JOIN calendar_day c USING (date)
            WHERE o.ring_id = 1 AND c.giants_home AND c.n_games = 1
              AND c.first_pitch_hour IS NOT NULL
            GROUP BY o.date, c.first_pitch_hour)
        SELECT COUNT(*), COUNT(*) FILTER (abs(peak_hour - first_pitch_hour) <= 3)
        FROM peaks""").fetchone()
    share = aligned / n_games if n_games else 0
    gate("advan_vbh_local_time", n_games > 0 and share >= 0.80,
         f"ring-1 peak hour within 3h of first pitch on {aligned}/{n_games} "
         f"single-game days ({share:.1%}); UTC misread would score ~0")

    # 2. PHYSICAL PLAUSIBILITY. visitor_hours / visits at the venue is the mean
    # number of whole-hour buckets a fan spans. ~2.8 hours on site produces ~4
    # buckets, so anything outside [2.5, 6.0] means the two columns are not
    # measuring what we think.
    lo, hi, med = con.sql(f"""
        WITH v AS (
            SELECT w.week_start, SUM(w.visits) AS visits,
                   SUM(list_sum(w.h)) AS vh
            FROM occ_weeks w
            JOIN poi_rings p USING (FOOTPRINT_ID)
            WHERE p.location_name = '{VENUE_POI_NAME}' AND w.has_hourly
            GROUP BY 1)
        SELECT ROUND(MIN(vh / visits), 2), ROUND(MAX(vh / visits), 2),
               ROUND(median(vh / visits), 2)
        FROM v WHERE visits > 0""").fetchone()
    gate("venue_hours_per_visit",
         med is not None and 2.5 <= med <= 6.0,
         f"venue visitor_hours/visits median={med} (weekly range {lo} to {hi}), "
         f"want median in [2.5, 6.0]")

    # 3. DWELL CROSS-CHECK. BUCKETED_DWELL_TIMES sums to VISIT_COUNTS and is an
    # independent signal. A visit of duration d starting at a random offset in
    # an hour spans about 1 + d/60 buckets, so bucket midpoints give per-visit
    # multipliers and a predicted visitor-hours total. Agreement is strong
    # evidence for bucket-spanning; disagreement means the interpretation is off.
    corr, ratio, n = con.sql("""
        WITH d AS (
            SELECT list_sum(h) AS observed,
                   TRY_CAST(json_extract(dwell, '$."<5"')     AS BIGINT) * 1.042
                 + TRY_CAST(json_extract(dwell, '$."5-20"')   AS BIGINT) * 1.208
                 + TRY_CAST(json_extract(dwell, '$."21-60"')  AS BIGINT) * 1.667
                 + TRY_CAST(json_extract(dwell, '$."61-240"') AS BIGINT) * 3.500
                 + TRY_CAST(json_extract(dwell, '$.">240"')   AS BIGINT) * 6.000
                     AS predicted
            FROM occ_weeks
            WHERE has_hourly AND dwell IS NOT NULL)
        SELECT ROUND(corr(observed, predicted), 4),
               ROUND(SUM(observed) / NULLIF(SUM(predicted), 0), 4),
               COUNT(*)
        FROM d WHERE predicted > 0""").fetchone()
    gate("advan_vbh_dwell_crosscheck",
         corr is not None and corr >= 0.85 and 0.7 <= ratio <= 1.4,
         f"corr(observed, dwell-predicted)={corr}, aggregate ratio={ratio} "
         f"over {n:,} POI-weeks; want corr>=0.85 and ratio in [0.7, 1.4]")


def crosscheck_bike_lift(con):
    """Reproduce the validate_join.py Aug-2024 contrast: Bay Wheels trips
    starting within 1 MILE (1609.344 m, the original check's definition) of
    the park, game vs non-game days. Reference: +9.3% (officialDate fix,
    2026-07-02). Computed from raw trips at a fixed radius on purpose, so
    this check stays faithful no matter how RING_EDGES_M changes."""
    if not con.sql("SELECT COUNT(*) FROM information_schema.tables "
                   "WHERE table_name = 'bike_trips'").fetchone()[0]:
        skipped("crosscheck_bike_lift", "no bikeshare data (non-local bronze)")
        return
    dist = haversine_sql("start_lat", "start_lng")
    row = con.sql(f"""
        WITH b AS (
            SELECT sdate AS date, COUNT(*) AS starts
            FROM bike_trips
            WHERE start_lat IS NOT NULL AND ({dist}) <= 1609.344
              AND sdate BETWEEN DATE '2024-08-01' AND DATE '2024-08-31'
            GROUP BY 1)
        SELECT AVG(starts) FILTER (c.giants_home),
               AVG(starts) FILTER (NOT c.giants_home)
        FROM b JOIN calendar_day c USING (date)
    """).fetchone()
    if not row or row[0] is None or row[1] is None:
        skipped("crosscheck_bike_lift", "no Aug-2024 game/non-game contrast available")
        return
    lift = (row[0] - row[1]) / row[1] * 100
    gate("crosscheck_bike_lift", 4 <= lift <= 16,
         f"Aug-2024 game-day lift at 1 mi = {lift:.1f}% (reference ~9.3%)")


MIN_LUKE_CORR = 0.90  # ring 1 (0-250m) is a subset of his 0-300m POI set


def crosscheck_luke_residuals(con, residuals):
    """Luke's game_residuals_0_300m `v` vs our ring-1 visits_food.

    His v = daily visits to NAICS-722 (food services) POIs within 0-300m; with
    the pre-metric 0-300m core ring this pipeline reproduced it EXACTLY
    (corr = 1.0000, ratio = 1.0000, established 2026-07-18). After the metric
    re-ring (core = 0-250m) ring 1 is a subset of his POI set, so expect corr
    high but below 1 and his/ours ratio above 1.

    Never fails the build: `residuals` belongs to the other pipeline and may
    legitimately be unreachable. But an unreachable file is recorded as DID NOT
    RUN, not as a pass. This is the only check tying the two pipelines together,
    and it used to be able to stop running without anyone noticing: the previous
    version wrote ok=True with the exception text as its detail, which the
    generated README then rendered as `PASS crosscheck_luke_v: skipped: ...`.
    """
    try:
        corr, ratio, n = con.sql(f"""
            SELECT corr(l.v, o.visits_food), AVG(l.v / o.visits_food), COUNT(*)
            FROM '{residuals}' l
            JOIN visits_ring_day o ON o.date = l.date AND o.ring_id = 1
        """).fetchone()
    except Exception as e:
        skipped("crosscheck_luke_v", f"{residuals} unreadable: {e}")
        return
    # A zero-row join is its own failure mode: corr comes back NULL and the old
    # f-string raised TypeError, landing in the same except and reading as a pass.
    if not n or corr is None or ratio is None:
        skipped("crosscheck_luke_v",
                f"no overlapping dates with {residuals} (n={n})")
        return
    crosscheck("crosscheck_luke_v", corr >= MIN_LUKE_CORR,
               f"n={n}, corr={corr:.4f}, mean(his v / our visits)={ratio:.4f}, "
               f"want corr >= {MIN_LUKE_CORR}")


TABLES = ["poi_rings", "visits_ring_day", "calendar_day", "weather_day",
          "transit_day", "bikeshare_ring_day", "panel_ring_day",
          "occupancy_ring_hour", "occupancy_poi_hour",
          "occupancy_poi_week_coverage", "weather_hour", "event_hour"]


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
        "params": {"ring_edges_m": RING_EDGES_M, "ring_labels": RING_LABELS,
                   "panel_start": PANEL_START, "panel_end": PANEL_END,
                   "occ_start": OCC_START, "occ_end": OCC_END,
                   "food_naics_prefix": FOOD_NAICS_PREFIX,
                   "event_pre_h": EVENT_PRE_H, "event_post_h": EVENT_POST_H,
                   "concert_default_hours": CONCERT_DEFAULT_HOURS,
                   "lcd_station": "USW00023234"},
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
    qa = qa_markdown(m["qa"])
    return f"""# silver/ - conformed, joined, analysis-grain tables

DERIVED DATA. Built only by `pipeline/build_silver.py` in the team repo
(su26-aai590-Group2); never hand-edited. To change anything here, change the
code or bronze and rebuild. This prefix is synced with `--delete`: it always
reflects exactly one build.

Grain: rings around Oracle Park (37.7786, -122.3893), edges (meters):
{m['params']['ring_edges_m']} -> rings {m['params']['ring_labels']}.
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

## Occupancy (visitor-hours) tables

`visitor_hours` is NOT a headcount. Advan dedupes unique visitors WITHIN each
bucket, and the bucket differs per column: week for `VISITOR_COUNTS`, day for
`VISITS_BY_DAY`, hour for `VISITS_BY_EACH_HOUR`. A visitor spanning four hourly
buckets counts in all four, so one unit is one estimated visitor present during
one hourly bucket. Dividing `visitor_hours` by visits gives mean buckets
spanned, about 4 at the ballpark. Never add visitor-hours to visits.

- `occupancy_ring_hour.parquet` - ring x date x hour, dense. The deliverable.
  Total / ex-venue / food / balanced, plus `poi_covered`, `poi_total`,
  `visit_share_covered`. Coverage is a POI-WEEK property so it does not vary
  within a date; it is repeated on all 24 hourly rows.
- `occupancy_poi_hour.parquet` - POI x date x hour, SPARSE (non-zero only).
  Individual POI-hours are quantized at roughly 12 (Advan's device scale-up), so
  do not read a single POI-hour as precise. Ring aggregates average this out.
- `occupancy_poi_week_coverage.parquet` - POI x week `has_hourly` flag. Required
  to read the sparse table: a missing hour is a TRUE ZERO when `has_hourly` is
  true, and UNKNOWN when it is false.
- `visitor_hours_balanced` uses POIs with hourly in >= 95% of window weeks and
  open the whole window. WEAK IN RING 1 (6 POIs; only ~21 of its 37 report
  hourly at all): prefer the raw + coverage columns there.

Hour-grain covariates (same occupancy window):

- `weather_hour.parquet` - NOAA LCD hourly temp/precip/wind, SFO only
  (downtown has no hourly obs), LST converted to wall clock.
- `event_hour.parquet` - SPARSE Chase Center event windows: NBA/WNBA real
  tip-offs -{m['params']['event_pre_h']}h..+{m['params']['event_post_h']}h,
  concerts default {m['params']['concert_default_hours']} (setlist.fm has no
  times). Moscone/citywide stay day-grain in calendar_day.
- `calendar_day.us_federal_holiday` - actual holiday dates, static in-code
  list.

Hourly is null for about 38% of POI-weeks and that is never imputed. The gap is
not random (74.9% of 10-99 visit POIs vs 11.0% of 1000+), so filling it would
bias small POIs. Weakest cell in the design: ring-1 food services rests on about
half its POIs and two thirds of its food visits.

OCCUPANCY WINDOW IS {m['params']['occ_start']} to {m['params']['occ_end']},
NARROWER than the visits panel. Advan changed the hourly construction at
2023 Q1: on a fixed POI set the hourly/daily ratio jumps +69% at the boundary
(1.64 -> 2.77), so 2022 visitor-hours are not comparable and are excluded. The
daily visits tables keep the full window; VISITS_BY_DAY shows no break. Gate
`advan_hourly_construction_stable` enforces this.

## This build

- built_at: {m['built_at']}
- git_sha: {m['git_sha']}
- bronze: {m['bronze']}

## QA log

Every silver check is an INTEGRITY gate: parse, array length, Monday alignment,
panel shape, unit conversions, coverage floors, and the local-vs-UTC hour
reading. A failure aborts the build, so if these tables exist the gates passed.
Silver asserts nothing about what the data SHOWS, so there are no result
expectations here (those live in gold). A check that DID NOT RUN is listed as
such and never counts as agreement.

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
    build_weather_hour(con, args.bronze)
    build_event_hour(con, args.bronze)
    build_transit_day(con, args.bronze)
    build_bikeshare_ring_day(con, args.bronze)
    build_panel(con, panel_end)
    build_occupancy(con, args.bronze)
    crosscheck_occupancy(con)
    crosscheck_bike_lift(con)
    crosscheck_luke_residuals(con, args.residuals)
    write_outputs(con, args.out, args.bronze, started)
    log(f"done in {(datetime.datetime.now() - started).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
