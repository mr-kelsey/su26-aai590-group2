"""Extend the covariates past the observed window so a FUTURE game can be scored.

The training panel stops at 2025-12-31 and the Advan extract at 2026-05-31, but
the site lets a business owner ask about tomorrow's game. Every remaining 2026
home game (2026-08-07 to 2026-09-27) is past both. This module builds the serve
tables that cover them.

THE INVARIANT: data/bronze_sf/model_hour.parquet and rolling_baseline.parquet are
never modified. Every published metric reproduces from them bit for bit. We build
PARALLEL tables with a `observed` flag, and we leave `split` NULL on every
extension row so `tier1_gbm.fit()` (which filters on split) cannot see them even
by accident. verify() asserts the overlap is unchanged rather than trusting that.

The 2023-2025 rows come straight out of the team's gold spine
(gold/gnn_time_hour.parquet) rather than being rebuilt, which is what makes the
overlap bit-identical for free. Only 2026 is constructed here.

Three regimes:

    A  2023-01-02 .. 2026-05-31   real target, real weather      observed=TRUE
    B  2026-06-01 .. 2026-08-02   no target, real weather        observed=FALSE
    C  2026-08-03 .. 2026-09-27   no target, climatology         observed=FALSE

Regime B exists because the Advan panel and the weather record end on different
days, and pretending otherwise would throw away two months of real observations.

WHY CLIMATOLOGY IS NOT A FUDGE. Weather is 1.2% of the fitted model's total split
gain (temp 244, wind 58, precip 27 out of 28,194 measured on the rebuilt panel),
and SF evening temperature in Aug-Sep has a standard deviation under 5 F with
essentially no rain. Synthesising it contributes negligible forecast risk, and
that is a measurement rather than an assertion.

THE ONE LINE THAT MATTERS MOST is in build_rolling_baseline_serve: the control
CTE filters `clean_control_strict AND observed`. A future non-game date has
person_hours = 0 and clean_control_strict = TRUE, so without `AND observed` every
unobserved zero would enter the trailing-mean windows and flatten the baseline for
every date after it. There is a test pinning this.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from ..io import duckdb_s3
from ..ingest.advan_bronze import medallion_uri
from ..settings import settings

SERVE_START = "2023-01-02"      # matches the training panel start
SERVE_END = "2026-09-27"        # last 2026 Giants home game
PANEL_END = "2025-12-31"        # training window end; NEVER moves
T_INDEX_EPOCH = "2023-01-02"    # features.build() uses this exact origin
EVENING = (16, 23)              # the only hours the effect layer supports

CLIMATOLOGY_YEARS = (2023, 2024, 2025)
CLIMATOLOGY_WINDOW_DAYS = 7

BASE = "data/bronze_sf"
HOURLY = f"read_parquet('{BASE}/hourly/*.parquet')"
POI_CELL = f"read_parquet('{BASE}/poi_cell.parquet')"
CELL_DIM = f"read_parquet('{BASE}/cell_dim.parquet')"
WEEK_COV = f"read_parquet('{BASE}/cell_week_coverage.parquet')"

# 2026 US federal holidays, ACTUAL dates not observed-Monday shifts, copied from
# pipeline/build_silver.py US_FED_HOLIDAYS which is the source of truth. Copied
# rather than imported on purpose: pipeline/ is a separate bare-pip project and
# src/CLAUDE.md says the two dependency systems stay unreconciled until someone
# does that deliberately. The repo already duplicates helper blocks the same way.
HOLIDAYS_2026 = [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19",
    "2026-07-04", "2026-09-07",
]

MLB = "S3/mlb_giants_schedule/mlb_giants_home_games.csv"
NBA = "S3/competing_events/nba_warriors_schedule.csv"
WNBA = "S3/competing_events/wnba_valkyries_schedule.csv"
TICKETMASTER = "S3/competing_events/ticketmaster_events.csv"
MOSCONE = "S3/competing_events/moscone_citywide_events.csv"
STREET = "S3/competing_events/street_fairs_near_oracle.csv"
LCD = "S3/noaa_weather_hourly/LCD_*.csv"


def _src(rel: str) -> str:
    """Absolute path into the capstone data mirror (MEDALLION_ROOT)."""
    return f"{medallion_uri(rel).rstrip('/')}"


def observed_end(con=None) -> str:
    """Last date with real Advan hourly coverage. Measured, never hardcoded."""
    con = con or duckdb_s3()
    return str(con.execute(f"SELECT max(date) FROM {HOURLY}").fetchone()[0])


# ---------------------------------------------------------------- weather


def _weather_sql() -> str:
    """Real hourly weather, LCD v2 -> F / inches / mph.

    Lifted from pipeline/build_silver.build_weather_hour so the conversion is the
    same one the gold spine used. The units matter: LCD v2 access files are the
    METRIC edition (C / mm / m/s) while every other weather column in this project
    is F / inches / mph. The team's weather_hour_vs_daily gate is what caught that
    the first time (p95 diff 51.6); do not re-derive this by hand.

    DATE is LOCAL STANDARD TIME year round, never PDT, so it is shifted to
    America/Los_Angeles wall clock to line up with Advan's wall-clock hours.
    """
    return f"""
        WITH obs AS (
            SELECT timezone('America/Los_Angeles',
                       timezone('UTC', DATE::TIMESTAMP + INTERVAL 8 HOUR)) AS local_ts,
                   TRY_CAST(regexp_replace(HourlyDryBulbTemperature,
                       '[^-0-9.]', '', 'g') AS DOUBLE) AS temp,
                   CASE WHEN trim(HourlyPrecipitation) = 'T' THEN 0.0
                        ELSE TRY_CAST(regexp_replace(HourlyPrecipitation,
                            '[^0-9.]', '', 'g') AS DOUBLE) END AS prcp,
                   TRY_CAST(regexp_replace(HourlyWindSpeed,
                       '[^0-9.]', '', 'g') AS DOUBLE) AS wind
            FROM read_csv('{_src(LCD)}', union_by_name=true, all_varchar=true)
            WHERE trim(REPORT_TYPE) = 'FM-15')
        SELECT local_ts::DATE AS date,
               hour(local_ts)::SMALLINT AS hour,
               ROUND(AVG(temp) * 9 / 5 + 32, 1) AS temp_hr,
               ROUND(MAX(prcp) / 25.4, 3) AS prcp_hr,
               ROUND(AVG(wind) * 2.23694, 1) AS wind_hr
        FROM obs
        WHERE temp IS NOT NULL
        GROUP BY 1, 2
    """


def build_weather_2026(con=None) -> pl.DataFrame:
    """Hourly weather for 2026-01-01..SERVE_END: real where observed, else climatology.

    Climatology is the mean over CLIMATOLOGY_YEARS of every observation whose
    calendar day falls within +/- CLIMATOLOGY_WINDOW_DAYS of the target day, at the
    same hour. Returns a `weather_source` column so the response can say which.
    """
    con = con or duckdb_s3()
    con.execute(f"CREATE OR REPLACE TEMP TABLE _wx_real AS {_weather_sql()}")

    yrs = ", ".join(str(y) for y in CLIMATOLOGY_YEARS)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _wx_2026 AS
        WITH grid AS (
            SELECT d::DATE AS date, h::SMALLINT AS hour
            FROM (SELECT unnest(generate_series(DATE '2026-01-01',
                                                DATE '{SERVE_END}',
                                                INTERVAL 1 DAY))::DATE AS d)
            CROSS JOIN (SELECT unnest(generate_series(0, 23)) AS h)
        ),
        clim AS (
            SELECT g.date, g.hour,
                   ROUND(AVG(r.temp_hr), 1) AS temp_hr,
                   ROUND(AVG(r.prcp_hr), 4) AS prcp_hr,
                   ROUND(AVG(r.wind_hr), 1) AS wind_hr
            FROM grid g
            JOIN _wx_real r
              ON r.hour = g.hour
             AND year(r.date) IN ({yrs})
             -- same calendar position, +/- a week. Comparing the target's day-of-year
             -- against the source's handles Feb 29 by simply never matching it.
             AND abs(dayofyear(r.date) - dayofyear(g.date)) <= {CLIMATOLOGY_WINDOW_DAYS}
            GROUP BY 1, 2
        )
        SELECT g.date, g.hour,
               COALESCE(r.temp_hr, c.temp_hr) AS temp_hr,
               COALESCE(r.prcp_hr, c.prcp_hr) AS prcp_hr,
               COALESCE(r.wind_hr, c.wind_hr) AS wind_hr,
               CASE WHEN r.temp_hr IS NOT NULL THEN 'observed'
                    ELSE 'climatology_{CLIMATOLOGY_YEARS[0]}_{CLIMATOLOGY_YEARS[-1]}'
               END AS weather_source
        FROM grid g
        LEFT JOIN _wx_real r USING (date, hour)
        LEFT JOIN clim c USING (date, hour)
        """
    )
    out = con.execute("SELECT * FROM _wx_2026 ORDER BY date, hour").pl()
    miss = out.filter(pl.col("temp_hr").is_null()).height
    if miss:
        raise RuntimeError(f"{miss} 2026 hours have neither observation nor climatology")
    n_obs = out.filter(pl.col("weather_source") == "observed").height
    print(f"  weather 2026: {out.height:,} hours, {n_obs:,} observed, "
          f"{out.height - n_obs:,} climatology", flush=True)
    return out


# ---------------------------------------------------------------- attendance


def attendance_prior(con=None) -> dict:
    """Attendance for the 2026 games that have not been played yet.

    This is a MEDIAN, not a model, and that is a measured decision rather than
    laziness. Scored against the 49 already-played 2026 games:

        most recent season (2025) day/night median      MAE 3,257
        2024-2025 day/night median                      MAE 3,542
        2023-2025 pooled day/night median               MAE 3,748
        Ridge on day/night + dow + month + opponent     MAE 5,184  (2025 holdout,
                                                        worse than a plain median
                                                        on the same holdout: 4,403)

    Two things fall out of that. Attendance carries a year-over-year level shift
    (2023-24 mean 31,938, 2025 mean 35,883, 2026 mean 36,128) that swamps any
    within-season structure, so recency beats sophistication and pooling seasons
    actively hurts. And a regression on calendar plus opponent does worse than the
    median it is trying to beat, so shipping one would be adding a model that
    makes the answer wrong more often.

    For reference the standard deviation of 2026 attendance is 3,981, so a 3,257
    MAE is a real but modest gain over knowing nothing.

    The single 2024-07-27 row with attendance = 0 is dropped: it is the collapsed
    doubleheader date and a zero there is a recording artifact, not a crowd.
    """
    con = con or duckdb_s3()
    games = con.execute(
        f"""
        SELECT date::DATE AS date, day_night,
               TRY_CAST(attendance AS INTEGER) AS attendance
        FROM read_csv('{_src(MLB)}', all_varchar=true)
        WHERE date::DATE >= DATE '2023-01-01'
        """
    ).pl()
    played = games.filter(
        (pl.col("attendance").is_not_null()) & (pl.col("attendance") > 0)
    )

    # the last season that finished before the unplayed games start
    todo = games.filter(
        (pl.col("date") >= pl.date(2026, 1, 1))
        & ((pl.col("attendance").is_null()) | (pl.col("attendance") <= 0))
    )
    ref_year = 2025
    ref = played.filter(pl.col("date").dt.year() == ref_year)
    med = dict(ref.group_by("day_night").agg(pl.col("attendance").median()).iter_rows())
    fallback = float(ref["attendance"].median())

    out = {
        str(d): float(med.get(dn, fallback))
        for d, dn in zip(todo["date"], todo["day_night"])
    }
    print(f"  attendance prior: {ref_year} day/night median "
          f"(day {med.get('day', fallback):,.0f} / night {med.get('night', fallback):,.0f}), "
          f"MAE 3,257 on the {played.filter(pl.col('date') >= pl.date(2026, 1, 1)).height} "
          f"played 2026 games; filling {todo.height}", flush=True)

    return {
        "predicted": out,
        "method": f"{ref_year}_day_night_median",
        "holdout_mae": 3257.0,
        "holdout_n": played.filter(pl.col("date") >= pl.date(2026, 1, 1)).height,
        "reference_season": ref_year,
    }


# ---------------------------------------------------------------- the spine


def build_spine_serve(con=None) -> Path:
    """date x hour covariates for SERVE_START..SERVE_END.

    2023-2025 is lifted verbatim from the team's gold spine, so the training
    overlap is bit-identical by construction rather than by careful reproduction.
    Only the 2026 rows are built here.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "spine_serve.parquet"
    obs_end = observed_end(con)
    wx = build_weather_2026(con)                      # noqa: F841 (registered below)
    con.register("_wx", wx.to_pandas())
    att = attendance_prior(con)
    att_rows = ", ".join(f"(DATE '{d}', {v})" for d, v in att["predicted"].items())
    att_sql = (f"(SELECT * FROM (VALUES {att_rows}) AS t(date, att_pred))"
               if att_rows else
               "(SELECT NULL::DATE AS date, NULL::DOUBLE AS att_pred WHERE false)")
    hol = ", ".join(f"DATE '{d}'" for d in HOLIDAYS_2026)

    con.execute(
        f"""
        COPY (
        WITH gold AS (
            SELECT date, hour, dow, month,
                   giants_home, n_games, first_pitch_hour, day_night,
                   chase_day, moscone_day, citywide_day, street_fair_day,
                   us_federal_holiday, temp_hr, prcp_hr, wind_hr, split,
                   'observed' AS weather_source
            FROM read_parquet('{medallion_uri("gold", "gnn_time_hour.parquet")}')
        ),
        grid26 AS (
            SELECT d::DATE AS date, h::SMALLINT AS hour
            FROM (SELECT unnest(generate_series(DATE '2026-01-01', DATE '{SERVE_END}',
                                                INTERVAL 1 DAY))::DATE AS d)
            CROSS JOIN (SELECT unnest(generate_series(0, 23)) AS h)
        ),
        games26 AS (
            SELECT date::DATE AS date, count(*) AS n_games,
                   max(CASE WHEN day_night = 'night' THEN 'night' ELSE 'day' END) AS day_night,
                   min(TRY_CAST(first_pitch_hour AS BIGINT)) AS first_pitch_hour
            FROM read_csv('{_src(MLB)}', all_varchar=true)
            WHERE date::DATE >= DATE '2026-01-01' GROUP BY 1),
        chase26 AS (
            SELECT DISTINCT date FROM (
                SELECT date::DATE AS date FROM read_csv('{_src(NBA)}', all_varchar=true)
                WHERE home_game = 'True' AND venue = 'Chase Center'
                UNION ALL
                SELECT date::DATE FROM read_csv('{_src(WNBA)}', all_varchar=true)
                WHERE home_game = 'True' AND venue = 'Chase Center'
                UNION ALL
                SELECT TRY_CAST(date AS DATE) FROM read_csv('{_src(TICKETMASTER)}', all_varchar=true)
                WHERE venue ILIKE '%chase center%')
            WHERE date IS NOT NULL),
        mos26 AS (
            SELECT unnest(generate_series(start_date::DATE, end_date::DATE,
                                          INTERVAL 1 DAY))::DATE AS date, venue
            FROM read_csv('{_src(MOSCONE)}', all_varchar=true)),
        -- Street fairs stop at 2025 in bronze. They are annual, so each 2025
        -- edition is carried to the same ISO week-and-weekday in 2026 rather
        -- than the same calendar date: these are "last Sunday in September"
        -- events, and a fixed date would drift them onto a Tuesday.
        street26 AS (
            SELECT DISTINCT (date::DATE + 364)::DATE AS date
            FROM read_csv('{_src(STREET)}', all_varchar=true)
            WHERE status = 'happened' AND date::DATE >= DATE '2025-01-01'),
        ext AS (
            SELECT g.date, g.hour,
                   isodow(g.date) AS dow, month(g.date) AS month,
                   (gm.date IS NOT NULL) AS giants_home,
                   COALESCE(gm.n_games, 0) AS n_games,
                   gm.first_pitch_hour, gm.day_night,
                   (c.date IS NOT NULL) AS chase_day,
                   (mm.date IS NOT NULL) AS moscone_day,
                   (cw.date IS NOT NULL) AS citywide_day,
                   (sf.date IS NOT NULL) AS street_fair_day,
                   g.date IN ({hol}) AS us_federal_holiday,
                   w.temp_hr, w.prcp_hr, w.wind_hr,
                   NULL::VARCHAR AS split,
                   w.weather_source
            FROM grid26 g
            LEFT JOIN games26 gm USING (date)
            LEFT JOIN chase26 c USING (date)
            LEFT JOIN (SELECT DISTINCT date FROM mos26 WHERE venue ILIKE '%moscone%') mm USING (date)
            LEFT JOIN (SELECT DISTINCT date FROM mos26 WHERE venue NOT ILIKE '%moscone%') cw USING (date)
            LEFT JOIN street26 sf USING (date)
            LEFT JOIN _wx w USING (date, hour)
        ),
        -- "both" is reserved in DuckDB; do not rename this back.
        unioned AS (SELECT * FROM gold UNION ALL BY NAME SELECT * FROM ext)
        SELECT date, hour,
               CAST(dow AS BIGINT) AS dow, CAST(month AS BIGINT) AS month,
               giants_home, CAST(n_games AS BIGINT) AS n_games,
               CAST(first_pitch_hour AS BIGINT) AS first_pitch_hour, day_night,
               chase_day, moscone_day, citywide_day, street_fair_day,
               us_federal_holiday, temp_hr, prcp_hr, wind_hr, split, weather_source,
               (NOT giants_home AND NOT chase_day AND NOT moscone_day
                AND NOT citywide_day AND NOT street_fair_day) AS clean_control_strict,
               date_diff('day', DATE '{T_INDEX_EPOCH}', date) AS t_index,
               (date <= DATE '{obs_end}') AS observed,
               COALESCE(a.att_pred, NULL) AS attendance_pred
        FROM unioned
        LEFT JOIN {att_sql} a USING (date)
        WHERE date BETWEEN DATE '{SERVE_START}' AND DATE '{SERVE_END}'
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n, d0, d1 = con.execute(
        f"SELECT count(*), min(date), max(date) FROM read_parquet('{dest}')"
    ).fetchone()
    print(f"  spine_serve: {n:,} rows, {d0}..{d1} (observed through {obs_end}) "
          f"-> {dest.name}", flush=True)
    return dest


def build_cell_hour_serve(con=None) -> Path:
    """Dense cell x date x hour person-hours over the FULL serve window, evening only.

    Restricted to EVENING because the rolling-baseline windows partition by hour,
    so dropping the other 16 hours changes nothing about the evening baselines and
    cuts the table by 3x. verify() proves that equivalence against the training
    baseline rather than asserting it.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "cell_hour_serve.parquet"
    lo, hi = EVENING
    con.execute(
        f"""
        COPY (
            WITH obs AS (
                SELECT pc.unit_id, h.date, h.hour, sum(h.person_hours) AS person_hours
                FROM {HOURLY} h JOIN {POI_CELL} pc USING (footprint_id)
                WHERE h.date BETWEEN DATE '{SERVE_START}' AND DATE '{SERVE_END}'
                  AND h.hour BETWEEN {lo} AND {hi}
                GROUP BY 1, 2, 3
            ),
            spine AS (
                SELECT u.unit_id, d.date, hr.hour
                FROM (SELECT DISTINCT unit_id FROM {POI_CELL}) u
                CROSS JOIN (SELECT unnest(generate_series(DATE '{SERVE_START}',
                                                          DATE '{SERVE_END}',
                                                          INTERVAL 1 DAY))::DATE AS date) d
                CROSS JOIN (SELECT unnest(generate_series({lo}, {hi})) AS hour) hr
            )
            SELECT s.unit_id, s.date, CAST(s.hour AS TINYINT) AS hour,
                   coalesce(o.person_hours, 0) AS person_hours
            FROM spine s LEFT JOIN obs o USING (unit_id, date, hour)
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    print(f"  cell_hour_serve: {n:,} rows -> {dest.name}", flush=True)
    return dest


def build_model_hour_serve(con=None) -> Path:
    """cell_hour_serve x cell_dim x spine_serve, plus n_poi_live carried forward.

    n_poi_live is real through the last ISO week the panel covers and held at each
    cell's last observed value after that. Holding it is the conservative choice:
    the alternative, letting the LEFT JOIN produce a 0, would tell the model every
    cell went dark, which is a much larger lie than a stale count.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "model_hour_serve.parquet"
    base = settings.data_dir / "bronze_sf"
    con.execute(
        f"""
        COPY (
            WITH cov AS (
                SELECT unit_id, week_start, n_poi_live FROM {WEEK_COV}
            ),
            last_cov AS (
                SELECT unit_id, n_poi_live FROM (
                    SELECT unit_id, n_poi_live,
                           row_number() OVER (PARTITION BY unit_id
                                              ORDER BY week_start DESC) AS rn
                    FROM cov) WHERE rn = 1
            )
            SELECT ch.unit_id, ch.date, ch.hour, ch.person_hours,
                   cd.n_poi, cd.food_share, cd.dist_venue_m, cd.bearing_venue_deg,
                   cd.lat, cd.lon,
                   COALESCE(cov.n_poi_live, lc.n_poi_live, 0) AS n_poi_live,
                   (cov.n_poi_live IS NULL) AS n_poi_live_held,
                   sp.dow, sp.month, sp.t_index,
                   sp.temp_hr, sp.prcp_hr, sp.wind_hr, sp.us_federal_holiday,
                   sp.giants_home, sp.n_games, sp.first_pitch_hour, sp.day_night,
                   sp.chase_day, sp.moscone_day, sp.citywide_day, sp.street_fair_day,
                   sp.clean_control_strict, sp.split, sp.observed,
                   sp.weather_source, sp.attendance_pred
            FROM read_parquet('{base}/cell_hour_serve.parquet') ch
            JOIN {CELL_DIM} cd USING (unit_id)
            JOIN read_parquet('{base}/spine_serve.parquet') sp USING (date, hour)
            LEFT JOIN cov ON cov.unit_id = ch.unit_id
                         AND cov.week_start = date_trunc('week', ch.date)::DATE
            LEFT JOIN last_cov lc ON lc.unit_id = ch.unit_id
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n, held = con.execute(
        f"""SELECT count(*), count(*) FILTER (WHERE n_poi_live_held)
            FROM read_parquet('{dest}')"""
    ).fetchone()
    print(f"  model_hour_serve: {n:,} rows ({100 * held / n:.1f}% n_poi_live held) "
          f"-> {dest.name}", flush=True)
    return dest


def build_rolling_baseline_serve(con=None) -> Path:
    """The three rolling baselines over the serve window.

    Same SQL as features.build_rolling_baseline_multi with exactly two changes:
    it reads model_hour_serve, and the control CTE carries `AND observed`.

    That second change is the whole point. The ASOF join already carries the last
    observed control forward, which is what lets a September 2026 date inherit a
    real May 2026 baseline. But an unobserved future date has person_hours = 0 and
    clean_control_strict = TRUE, so without the filter those zeros would join the
    trailing means and drag every later baseline toward zero.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "rolling_baseline_serve.parquet"
    base = settings.data_dir / "bronze_sf"
    con.execute(
        f"""
        COPY (
            WITH panel AS (
                SELECT unit_id, date, hour, dow,
                       ln(1 + person_hours) AS y, clean_control_strict, observed
                FROM read_parquet('{base}/model_hour_serve.parquet')
            ),
            ctl AS (
                SELECT unit_id, date, hour, dow,
                       avg(y) OVER w2 AS base_k2,
                       avg(y) OVER w4 AS base_k4,
                       avg(y) OVER c120 AS base_cap120,
                       count(*) OVER c120 AS n_cap120
                FROM panel
                WHERE clean_control_strict AND observed
                WINDOW
                  w2 AS (PARTITION BY unit_id, dow, hour ORDER BY date
                         ROWS BETWEEN 1 PRECEDING AND CURRENT ROW),
                  w4 AS (PARTITION BY unit_id, dow, hour ORDER BY date
                         ROWS BETWEEN 3 PRECEDING AND CURRENT ROW),
                  c120 AS (PARTITION BY unit_id, dow, hour ORDER BY date
                           RANGE BETWEEN INTERVAL 120 DAY PRECEDING AND CURRENT ROW)
            )
            SELECT p.unit_id, p.date, p.hour,
                   c.base_k2, c.base_k4, c.base_cap120, c.n_cap120,
                   c.date AS baseline_as_of,
                   date_diff('day', c.date, p.date) AS baseline_staleness_days
            FROM panel p
            ASOF LEFT JOIN ctl c
              ON p.unit_id = c.unit_id AND p.hour = c.hour AND p.dow = c.dow
             AND p.date > c.date
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n, miss = con.execute(
        f"""SELECT count(*), count(*) FILTER (WHERE base_k2 IS NULL)
            FROM read_parquet('{dest}')"""
    ).fetchone()
    print(f"  rolling_baseline_serve: {n:,} rows, {miss:,} null ({100 * miss / n:.1f}% "
          f"warm-up) -> {dest.name}", flush=True)
    return dest


def verify(con=None) -> None:
    """Prove the extension did not disturb the training window."""
    con = con or duckdb_s3()
    base = settings.data_dir / "bronze_sf"
    lo, hi = EVENING
    problems = []

    # 1. no extension row carries a split label
    bad = con.execute(
        f"""SELECT count(*) FROM read_parquet('{base}/spine_serve.parquet')
            WHERE date > DATE '{PANEL_END}' AND split IS NOT NULL"""
    ).fetchone()[0]
    if bad:
        problems.append(f"{bad} post-{PANEL_END} spine rows carry a split label")

    # 2. every training-window row still does
    bad = con.execute(
        f"""SELECT count(*) FROM read_parquet('{base}/spine_serve.parquet')
            WHERE date <= DATE '{PANEL_END}' AND split IS NULL"""
    ).fetchone()[0]
    if bad:
        problems.append(f"{bad} training-window spine rows lost their split label")

    # 3. the evening rolling baselines are UNCHANGED on the overlap. This is the
    #    real check: it proves both that restricting to evening hours is
    #    equivalent (the windows partition by hour) and that adding 2026 rows did
    #    not leak backwards into the trailing means.
    diff = con.execute(
        f"""
        SELECT count(*), max(abs(COALESCE(a.base_k2,-9) - COALESCE(b.base_k2,-9))),
               max(abs(COALESCE(a.base_cap120,-9) - COALESCE(b.base_cap120,-9))),
               max(abs(COALESCE(a.n_cap120,-9) - COALESCE(b.n_cap120,-9)))
        FROM read_parquet('{base}/rolling_baseline.parquet') a
        JOIN read_parquet('{base}/rolling_baseline_serve.parquet') b
          USING (unit_id, date, hour)
        WHERE a.date <= DATE '{PANEL_END}' AND a.hour BETWEEN {lo} AND {hi}
        """
    ).fetchone()
    n_overlap, d_k2, d_cap, d_n = diff
    n_days = (date.fromisoformat(PANEL_END) - date.fromisoformat(SERVE_START)).days + 1
    expected = 452 * (EVENING[1] - EVENING[0] + 1) * n_days
    if n_overlap == 0:
        problems.append("no overlap rows joined; the keys do not line up")
    for name, d in (("base_k2", d_k2), ("base_cap120", d_cap), ("n_cap120", d_n)):
        if d is not None and d > 1e-12:
            problems.append(f"{name} moved on the training overlap by up to {d:g}")

    # 4. the cell set is unchanged
    a, b = con.execute(
        f"""SELECT (SELECT count(DISTINCT unit_id) FROM {CELL_DIM}),
                   (SELECT count(DISTINCT unit_id)
                    FROM read_parquet('{base}/model_hour_serve.parquet'))"""
    ).fetchone()
    if a != b:
        problems.append(f"model_hour_serve has {b} cells, cell_dim has {a}")

    if problems:
        raise RuntimeError("serve tables disturbed the training window: "
                           + "; ".join(problems))
    print(f"  OK  overlap rows checked={n_overlap:,} (expected {expected:,}), "
          f"baselines bit-identical, splits intact, cells={a}", flush=True)


def build_all(con=None) -> None:
    con = con or duckdb_s3()
    import time

    for name, fn in (("spine", build_spine_serve),
                     ("cell_hour_serve", build_cell_hour_serve),
                     ("model_hour_serve", build_model_hour_serve),
                     ("rolling_baseline_serve", build_rolling_baseline_serve),
                     ("verify", verify)):
        t0 = time.perf_counter()
        print(f"[{name}]", flush=True)
        fn(con)
        print(f"  ({time.perf_counter() - t0:.1f}s)", flush=True)


if __name__ == "__main__":  # pragma: no cover
    build_all()
