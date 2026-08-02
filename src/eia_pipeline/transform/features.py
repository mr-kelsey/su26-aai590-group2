"""Model-ready feature table: cell_hour joined to the hourly covariate spine.

We take the covariates from gold/gnn_time_hour rather than rebuilding them out of silver. That table already carries weather, event flags, relative-hour and the train/val/test split, and reusing it means our splits are identical to the team's GNN work, so our numbers are directly comparable to theirs. It is a 26,280-row spine, so the join costs us nothing.

We carry two definitions of a control day, because we wanted to measure the difference between them:

  clean_control         as shipped in gold. Excludes Giants games, but not Chase, Moscone, citywide events or street fairs.
  clean_control_strict  also excludes all of those.

828 days carry the shipped flag but only 581 are event-free, leaving about 247 contaminated days in the control pool. Measured at city grain, the difference between the two pools is -0.3% on test MAE. We keep both flags so the choice of pool is a measured result.

We also carry t_index so a model can absorb the 27% hourly construction slide instead of attributing it to economics. Panel health enters separately as n_poi_live, built in cell_week_coverage, because the hour-grain reporting count leaks the target.
"""
from __future__ import annotations

from pathlib import Path

from ..io import duckdb_s3
from ..settings import settings
from ..ingest.advan_bronze import medallion_uri

def time_spine() -> str:
    """Path to gold/gnn_time_hour. Lazy for the same reason as bronze_patterns()."""
    return medallion_uri("gold", "gnn_time_hour.parquet")
CELL_HOUR = "data/bronze_sf/cell_hour.parquet"
CELL_DIM = "data/bronze_sf/cell_dim.parquet"


def build(con=None) -> Path:
    """Join cell_hour x cell_dim x the gold hourly covariate spine into model_hour.

    One row per cell-hour, so 452 x 26,280 = 11,878,560 of them. Each row carries the target, both control definitions, the treatment and competing-event flags, weather, the cell statics and the split label. This is the single table that both of our model tiers read from.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "model_hour.parquet"
    con.execute(
        f"""
        COPY (
            SELECT
                ch.unit_id, ch.date, ch.hour,
                ch.person_hours, ch.n_poi_reporting,
                -- cell statics
                cd.n_poi, cd.food_share, cd.dist_venue_m, cd.bearing_venue_deg,
                cd.lat, cd.lon,
                -- treatment
                t.giants_home, t.n_games, t.first_pitch_hour, t.day_night,
                t.relative_hour, t.attendance_proxy,
                -- competing events
                t.chase_day, t.chase_event_hour, t.moscone_day,
                t.citywide_day, t.street_fair_day, t.us_federal_holiday,
                -- calendar / weather
                t.dow, t.month, t.temp_hr, t.prcp_hr, t.wind_hr,
                t.tmax, t.prcp,
                -- controls: shipped vs strict (see module docstring)
                t.clean_control,
                (NOT t.giants_home AND NOT t.chase_day AND NOT t.moscone_day
                 AND NOT t.citywide_day AND NOT t.street_fair_day)
                    AS clean_control_strict,
                t.split,
                -- drift covariate: days since window start
                date_diff('day', DATE '2023-01-02', ch.date) AS t_index
            FROM read_parquet('{CELL_HOUR}') ch
            JOIN read_parquet('{CELL_DIM}') cd USING (unit_id)
            JOIN (
                SELECT date, hour, giants_home, n_games, first_pitch_hour,
                       day_night, relative_hour,
                       NULL::DOUBLE AS attendance_proxy,
                       chase_day, chase_event_hour, moscone_day, citywide_day,
                       street_fair_day, us_federal_holiday, dow, month,
                       temp_hr, prcp_hr, wind_hr, tmax, prcp,
                       clean_control, split
                FROM read_parquet('{time_spine()}')
            ) t USING (date, hour)
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    n = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
    print(f"  model_hour: {n:,} rows -> {dest.name}", flush=True)
    return dest


def build_rolling_baseline_multi(con=None) -> Path:
    """Three rolling baselines at different lookback depths.

    A single deep baseline reaches too far back, because we skip event days when building it. With 514 of our 1,095 days excluded (246 games plus 268 other-event days), roughly 47% of the same-weekday candidates get skipped, so eight control days spans a median of 112 calendar days on game days, with a p90 of 140 and a maximum of 203. A Thursday in September would be partly baselined on Thursdays back in May.

    We measured baseline quality on the test-split control hours. RMSE is the criterion that matches the GBM's L2 objective. MAE on its own points at k=2, because it is robust to the noise a tiny k introduces:

        k     MAE      corr     RMSE
        2   0.8540   0.8483   1.5185
        3   0.8685   0.8545   1.4781
        4   0.8895   0.8549   1.4721   <- interior optimum
        8   0.9717   0.8456   1.5092
        mean(2,4,8)                     1.4352
        mean(2,4,cap120)                1.4347

    RMSE turns out to have an interior optimum around k=3-4, and combining depths beats any single one of them. The depths correlate 0.93-0.97, so they overlap but are not the same signal. Three is where the curve flattens out. Going to six depths only moves RMSE from 1.4352 to 1.4324.

    We apply the 120-day ceiling as a RANGE window rather than a row count, so it degrades to however many controls fit inside it, a median of 9. It costs nothing on accuracy (1.4347 against 1.4352) and removes the 203-day tail.

    We emit all three separately so the model can learn how to weight them. The numbers above are equal-weight means, so they are a lower bound on what the model gets.
    """
    con = con or duckdb_s3()
    dest = settings.data_dir / "bronze_sf" / "rolling_baseline.parquet"
    con.execute(
        f"""
        COPY (
            WITH panel AS (
                SELECT unit_id, date, hour, dow,
                       ln(1 + person_hours) AS y, clean_control_strict
                FROM read_parquet('{settings.data_dir}/bronze_sf/model_hour.parquet')
            ),
            ctl AS (
                SELECT unit_id, date, hour, dow,
                       avg(y) OVER w2 AS base_k2,
                       avg(y) OVER w4 AS base_k4,
                       avg(y) OVER c120 AS base_cap120,
                       count(*) OVER c120 AS n_cap120
                FROM panel WHERE clean_control_strict
                WINDOW
                  w2 AS (PARTITION BY unit_id, dow, hour ORDER BY date
                         ROWS BETWEEN 1 PRECEDING AND CURRENT ROW),
                  w4 AS (PARTITION BY unit_id, dow, hour ORDER BY date
                         ROWS BETWEEN 3 PRECEDING AND CURRENT ROW),
                  c120 AS (PARTITION BY unit_id, dow, hour ORDER BY date
                           RANGE BETWEEN INTERVAL 120 DAY PRECEDING AND CURRENT ROW)
            )
            SELECT p.unit_id, p.date, p.hour,
                   c.base_k2, c.base_k4, c.base_cap120, c.n_cap120
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
    print(f"  rolling_baseline (k2/k4/cap120): {n:,} rows, {miss:,} null "
          f"({100 * miss / n:.1f}% warm-up) -> {dest.name}", flush=True)
    return dest


def control_pool_summary(con=None) -> None:
    """Quantify the shipped-vs-strict control pool at day grain."""
    con = con or duckdb_s3()
    print(
        con.execute(
            f"""
            SELECT count(DISTINCT date) AS all_days,
                   count(DISTINCT date) FILTER (WHERE giants_home) AS giants,
                   count(DISTINCT date) FILTER (WHERE clean_control) AS shipped_control,
                   count(DISTINCT date) FILTER (
                       WHERE NOT giants_home AND NOT chase_day AND NOT moscone_day
                         AND NOT citywide_day AND NOT street_fair_day
                   ) AS strict_control
            FROM read_parquet('{time_spine()}')
            """
        ).df().to_string(index=False)
    )
