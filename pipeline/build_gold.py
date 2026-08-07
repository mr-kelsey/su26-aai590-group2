#!/usr/bin/env python3
"""
build_gold.py - build the GOLD layer (model-ready answers) from SILVER.

SCAFFOLD (2026-07-18): steps 1-3 work end to end; steps 4-5 are documented
stubs. Reads ONLY silver, never bronze. Full rebuild, QA gates, manifest,
same discipline as build_silver.py.

Gold products:
  game_effects.parquet      game-day x ring x measure: observed visits,
                            matched-control baseline, lift, lift_pct, plus
                            game covariates. THE building block. [WORKS]
  event_study_ring.parquet  ring x measure x slice: pooled lift with SEs
                            (slices: all, day, night, attendance terciles).
                            [WORKS]
  distance_decay.parquet    ring x measure: pct lift by distance; log-linear
                            decay fit stored in the build manifest. [WORKS]
  dollars_per_visit / game_impact_dollars                [STUB, see below]
  impact_model/                                          [STUB, see below]

Baseline estimator (v0, deliberately transparent): for each game day, ring,
and measure, the baseline is the MEAN over the MATCH_K nearest matched
clean-control days: same day-of-week, clean_control = true (no Giants game,
no ballpark event), within MATCH_MAX_WINDOW_DAYS. Nearest-K keeps the
comparison window tight in the calendar while guaranteeing coverage (a fixed
42-day window left 11 game dates, mostly Saturdays in dense homestand
stretches, with fewer than 5 controls). This mirrors Luke's exp/resid construction in
eia-nowcast (his exp was a same-dow control-mean; his 0-300m food series is
silver's ring-1 visits_food). A regression baseline (seasonality + weather +
confounder covariates) is the planned v1 upgrade and a natural experiment
for the agent loop; slot it in behind the same game_effects schema.

Promotion gate: this script writes LOCALLY. Publishing to
s3://aai-590-group2-capstone/gold/ is a human decision (the build plan's
model/spec promotion gate). Agent-loop experiment variants should publish to
experiments/<run-id>/ instead, never straight to gold/.

Run (Steve's machine):
  cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2 && \
    /Users/Steve3/Projects/personal/capstone/notebooks/.venv/bin/python \
    pipeline/build_gold.py \
    --silver /Users/Steve3/Projects/personal/capstone/silver \
    --out /Users/Steve3/Projects/personal/capstone/gold

Publish AFTER team promotion (gold is derived: --delete):
  aws s3 sync /Users/Steve3/Projects/personal/capstone/gold \
    s3://aai-590-group2-capstone/gold --delete --region us-east-2

Dependencies: duckdb only.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

import duckdb

# ---------------------------------------------------------------- parameters

MEASURES = ["visits", "visits_ex_venue", "visits_food", "visits_balanced"]
MATCH_K = 8               # nearest-K same-dow clean-control days per game
MATCH_MAX_WINDOW_DAYS = 120  # hard cap on how far a control may sit from the game
MIN_CONTROLS = 5          # game-ring cells with fewer matches get NULL baseline
RING_MID_M = {1: 125, 2: 375, 3: 750, 4: 1750, 5: 3750}  # ring midpoints, meters

# Hourly occupancy event study (visitor-hours; window is silver's OCC_*)
OCC_MEASURES = ["visitor_hours", "visitor_hours_ex_venue",
                "visitor_hours_food", "visitor_hours_balanced"]
REL_HOUR_MIN = -6         # hours before first pitch
REL_HOUR_MAX = 8          # hours after first pitch (crosses midnight for night games)

# GNN data contract (docs/design/2026-07-28-advan-occupancy.md section 8b)
VENUE_POI_NAME = "Oracle Park"  # must match build_silver.VENUE_POI_NAME
GNN_MAX_RING = 4          # nodes = POIs inside 2.5 km (rings 1-4)
K_SPATIAL = 8             # spatial k-nearest-neighbor edges per node
K_CATCH = 8               # catchment-similarity edges per node
MIN_COS = 0.1             # floor on catchment cosine similarity
SPLITS = {"train": ("2023-01-02", "2024-12-31"),
          "val":   ("2025-01-01", "2025-06-30"),
          "test":  ("2025-07-01", "2025-12-31")}

QA = {}


def log(msg):
    print(f"[build_gold] {msg}", flush=True)


def count_split_boundary_leaks(con, table="gnn_time_hour"):
    """Game hours whose split label disagrees with the split their date falls in.

    This is the check the old `gnn_no_leakage` gate was supposed to be. It used to
    read `clean_control AND giants_home`, but silver builds `giants_home` as
    `game.date IS NOT NULL` and `clean_control` as `game.date IS NULL AND ...`, so
    the two are mutually exclusive by construction and the count was structurally
    always zero. It reported PASS on a condition no row could ever meet.

    What can actually go wrong is a game hour landing in the wrong split, or in a
    split whose date range does not contain it, which is what leaks a test-window
    game into training. Rows outside every declared range count as leaks too, so
    widening the panel without widening SPLITS fails here instead of silently.
    """
    cases = " ".join(
        f"WHEN date BETWEEN DATE '{lo}' AND DATE '{hi}' THEN '{name}'"
        for name, (lo, hi) in SPLITS.items())
    return con.sql(f"""
        SELECT COUNT(*) FROM {table}
        WHERE giants_home
          AND split IS DISTINCT FROM (CASE {cases} ELSE NULL END)""").fetchone()[0]


# --------------------------------------------------------------- QA vocabulary
#
# Five kinds, because "PASS" used to stand for five different things and a
# swallowed exception was indistinguishable from a real pass in the manifest.
#
#   gate         integrity/shape/units/coverage. HARD: aborts the build.
#   expect       a claim about what the data SHOWS. Soft: recorded, never aborts.
#   crosscheck   agreement with an external artifact. Soft.
#   skipped      the check did not run, and why. Never counts as agreement.
#   note         provenance only, no verdict.
#
# The gate/expect split is the important one. Gating on a finding means a null
# or contrary result crashes the build, and any estimator change that shrinks
# the effect (better controls, a wider window, a fixed bootstrap) presents as
# broken plumbing rather than as a result. Gate the plumbing; report the finding.

def gate(name, ok, detail):
    """Integrity gate. Aborts the build on failure.

    For statements about whether the data was read and assembled correctly:
    row counts, join shapes, units, coverage floors, leakage checks. A claim
    about what the data shows belongs in expect(), not here.
    """
    QA[name] = {"kind": "gate", "ok": bool(ok), "detail": detail}
    log(f"GATE {'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        sys.exit(f"QA gate failed: {name}: {detail}")


def expect(name, ok, detail):
    """Expectation about a RESULT. Recorded and logged; never aborts.

    These encode what we expect to see if the hypothesis holds (core-ring lift
    positive, decay monotone in distance, placebo ring null). An unmet
    expectation is a finding to report and discuss with the team, so the build
    still writes every table and the manifest carries the verdict.
    """
    QA[name] = {"kind": "expectation", "ok": bool(ok), "detail": detail}
    log(f"EXPECT {'MET' if ok else 'NOT MET'} {name}: {detail}")
    if not ok:
        log(f"WARNING {name} not met. Tables still written; read the manifest "
            f"before quoting this build.")


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
    ("expectation", True): "MET", ("expectation", False): "NOT MET",
    ("crosscheck", True): "AGREES", ("crosscheck", False): "DISAGREES",
    ("skipped", False): "DID NOT RUN", ("skipped", True): "DID NOT RUN",
}

_QA_SECTIONS = [
    ("gate", "Integrity gates (a failure aborts the build)"),
    ("expectation", "Result expectations (recorded; they never abort the build)"),
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
            # A few details are structured (decay_fits): JSON reads in markdown,
            # a Python dict repr does not.
            detail = v["detail"]
            if not isinstance(detail, str):
                detail = json.dumps(detail, default=str)
            out.append(f"- {verdict + ' ' if verdict else ''}`{k}`: {detail}")
        out.append("")
    for kind, one, many, tail in (
        ("expectation", "result expectation was NOT MET",
         "result expectations were NOT MET",
         "Every table below was still written. Do not quote this build without "
         "reading why."),
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


def git_sha():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or None
    except Exception:
        return None


# ----------------------------------------------------------------- the build

def load_panel(con, silver):
    con.sql(f"""
        CREATE OR REPLACE TABLE panel AS
        SELECT * FROM '{silver.rstrip('/')}/panel_ring_day.parquet'
    """)
    n, games, controls = con.sql("""
        SELECT COUNT(*), COUNT(DISTINCT date) FILTER (giants_home),
               COUNT(DISTINCT date) FILTER (clean_control)
        FROM panel""").fetchone()
    log(f"panel: {n} ring-days, {games} game days, {controls} clean-control days")
    gate("panel_loaded", games > 300 and controls > 900,
         f"{games} game days, {controls} control days")


def build_game_effects(con):
    """Step 1: per game-day x ring x measure, matched-control baseline + lift.
    Long format over measures so downstream code never hardcodes columns."""
    measure_rows = " UNION ALL ".join(
        f"SELECT date, ring_id, ring, '{m}' AS measure, {m}::DOUBLE AS value, "
        f"giants_home, clean_control, dow FROM panel"
        for m in MEASURES
    )
    con.sql(f"CREATE OR REPLACE TABLE long_panel AS {measure_rows}")

    con.sql(f"""
        CREATE OR REPLACE TABLE game_effects AS
        WITH games AS (
            SELECT date, ring_id, ring, measure, value AS observed, dow
            FROM long_panel WHERE giants_home),
        matched AS (
            SELECT date, ring_id, measure,
                   AVG(value) AS baseline,
                   STDDEV_SAMP(value) AS baseline_sd,
                   COUNT(value) AS baseline_n
            FROM (
                SELECT g.date, g.ring_id, g.measure, c.value,
                       row_number() OVER (
                           PARTITION BY g.date, g.ring_id, g.measure
                           ORDER BY abs(date_diff('day', g.date, c.date)), c.date) AS rk
                FROM games g
                JOIN long_panel c
                  ON c.ring_id = g.ring_id AND c.measure = g.measure
                 AND c.clean_control AND c.dow = g.dow
                 AND abs(date_diff('day', g.date, c.date)) <= {MATCH_MAX_WINDOW_DAYS}
                 AND c.value IS NOT NULL)
            WHERE rk <= {MATCH_K}
            GROUP BY 1, 2, 3),
        cov AS (
            SELECT date, ANY_VALUE(n_games) AS n_games,
                   ANY_VALUE(attendance) AS attendance,
                   ANY_VALUE(day_night) AS day_night,
                   ANY_VALUE(first_pitch_hour) AS first_pitch_hour,
                   ANY_VALUE(opponents) AS opponents,
                   ANY_VALUE(game_types) AS game_types,
                   ANY_VALUE(dow) AS dow,
                   ANY_VALUE(chase_event) IS NOT NULL AS chase_day,
                   ANY_VALUE(moscone_event) IS NOT NULL AS moscone_day,
                   ANY_VALUE(tmax) AS tmax, ANY_VALUE(prcp) AS prcp
            FROM panel WHERE giants_home GROUP BY 1)
        SELECT g.date, g.ring_id, g.ring, g.measure, g.observed,
               CASE WHEN m.baseline_n >= {MIN_CONTROLS} THEN m.baseline END AS baseline,
               m.baseline_sd, m.baseline_n,
               g.observed - CASE WHEN m.baseline_n >= {MIN_CONTROLS} THEN m.baseline END AS lift,
               CASE WHEN m.baseline_n >= {MIN_CONTROLS}
                    THEN 100.0 * (g.observed - m.baseline) / NULLIF(m.baseline, 0)
               END AS lift_pct,
               cov.* EXCLUDE (date)
        FROM games g
        LEFT JOIN matched m USING (date, ring_id, measure)
        LEFT JOIN cov ON cov.date = g.date
    """)
    n, thin, ring1_food = con.sql(f"""
        SELECT COUNT(*),
               COUNT(*) FILTER (baseline IS NULL),
               ROUND(AVG(lift) FILTER (ring_id = 1 AND measure = 'visits_food'), 1)
        FROM game_effects""").fetchone()
    log(f"game_effects: {n} rows, ring-1 food mean lift = {ring1_food}")
    gate("game_effects_coverage", thin == 0,
         f"{thin} game-ring-measure cells with < {MIN_CONTROLS} matched controls")
    expect("core_ring_positive_lift", ring1_food is not None and ring1_food > 0,
           f"ring-1 visits_food mean lift = {ring1_food}")


def build_event_study(con):
    """Step 2: pool per-game lifts into per-ring effects, with slices."""
    con.sql("""
        CREATE OR REPLACE TABLE sliced AS
        SELECT *, 'all' AS slice FROM game_effects
        UNION ALL
        SELECT *, day_night AS slice FROM game_effects WHERE day_night IS NOT NULL
        UNION ALL
        -- terciles are a per-GAME property: rank distinct game days, then fan
        -- the label back out, so one game's ring/measure rows can never
        -- straddle two terciles (ntile over the long table did exactly that,
        -- nondeterministically, at tercile boundaries)
        SELECT g.*, 'att_t' || t.t AS slice
        FROM game_effects g
        JOIN (SELECT date, ntile(3) OVER (ORDER BY attendance, date) AS t
              FROM (SELECT DISTINCT date, attendance FROM game_effects
                    WHERE attendance IS NOT NULL)) t USING (date)
    """)
    con.sql("""
        CREATE OR REPLACE TABLE event_study_ring AS
        SELECT ring_id, ring, measure, slice,
               COUNT(lift) AS n_games,
               ROUND(AVG(lift), 1) AS mean_lift,
               ROUND(STDDEV_SAMP(lift) / sqrt(COUNT(lift)), 1) AS se_lift,
               ROUND(AVG(lift_pct), 2) AS mean_lift_pct,
               ROUND(STDDEV_SAMP(lift_pct) / sqrt(COUNT(lift_pct)), 2) AS se_lift_pct
        FROM sliced
        GROUP BY 1, 2, 3, 4
        ORDER BY measure, slice, ring_id
    """)
    core = con.sql("""
        SELECT mean_lift, se_lift, mean_lift_pct FROM event_study_ring
        WHERE ring_id = 1 AND measure = 'visits_food' AND slice = 'all'
    """).fetchone()
    # Guarded rather than indexed blind: now that an unmet expectation no longer
    # exits, a missing row must report itself instead of raising a TypeError two
    # lines later and taking the whole build down anyway.
    if core is None:
        expect("core_effect_significant", False,
               "no ring-1 visits_food slice=all row in event_study_ring")
    else:
        tstat = core[0] / core[1] if core[1] else 0.0
        expect("core_effect_significant", tstat > 4,
               f"ring-1 visits_food: {core[0]} +/- {core[1]} ({core[2]}%), "
               f"t = {tstat:.1f}")


def build_distance_decay(con):
    """Step 3: pct lift by ring distance + a log-linear decay fit."""
    mid_case = " ".join(f"WHEN {k} THEN {v}" for k, v in RING_MID_M.items())
    con.sql(f"""
        CREATE OR REPLACE TABLE distance_decay AS
        SELECT ring_id, ring, measure,
               CASE ring_id {mid_case} END AS ring_mid_m,
               n_games, mean_lift, se_lift, mean_lift_pct, se_lift_pct
        FROM event_study_ring
        WHERE slice = 'all'
        ORDER BY measure, ring_id
    """)
    fits = {}
    for m in MEASURES:
        row = con.sql(f"""
            SELECT regr_slope(ln(mean_lift_pct), ring_mid_m / 1000.0),
                   regr_intercept(ln(mean_lift_pct), ring_mid_m / 1000.0),
                   COUNT(*)
            FROM distance_decay
            WHERE measure = '{m}' AND mean_lift_pct > 0
        """).fetchone()
        fits[m] = {"log_slope_per_km": row[0], "log_intercept": row[1],
                   "rings_used": row[2]}
    note("decay_fits", fits)
    inner, outer = con.sql("""
        SELECT MAX(mean_lift_pct) FILTER (ring_id = 1),
               MAX(mean_lift_pct) FILTER (ring_id = 5)
        FROM distance_decay WHERE measure = 'visits_food'
    """).fetchone()
    expect("decay_direction",
           inner is not None and outer is not None and inner > outer,
           f"visits_food pct lift: ring 1 = {inner}, ring 5 = {outer}")


def build_occupancy_event_study(con, silver):
    """Hourly event study on VISITOR-HOURS (occupancy), 2023+ window.

    visitor_hours is presence per hourly bucket, not a headcount (a fan at a
    3-hour game counts in ~4 buckets); see the silver README and the design
    doc. Estimator mirrors game_effects v0 exactly, one addition: controls
    are read at the treated hour's offset from the game date's midnight on
    the nearest clean same-dow ANCHOR days, so the counterfactual for 19:00
    on a game Friday is 19:00 on the nearest clean Fridays.

    Three hour-grain traps handled here:
    - relative_hour is computed on TIMESTAMPS. A 19:00 first pitch + 8 hours
      is 03:00 the NEXT calendar day; integer hour math within a date would
      mislabel it.
    - Controls must cross midnight the same way. Matching on clock hour
      within a clean control DATE would baseline a night game's Sat-01:00
      (+6) on Fri-01:00, which is Thursday night's tail, and would let a
      previous evening's game egress contaminate control hours 0-3. The
      anchor join instead reads 01:00 on the day AFTER a clean Friday.
      Within-day hours select identical controls under both schemes.
    - Doubleheaders (n_games > 1) are excluded: two first pitches make
      relative_hour undefined. The day-grain code's MIN(first_pitch_hour) is
      harmless at day grain, wrong here.
    """
    con.sql("""
        CREATE OR REPLACE TABLE panel_dates AS
        SELECT DISTINCT date, giants_home, n_games, first_pitch_hour,
               day_night, clean_control, dow
        FROM panel
    """)
    con.sql(f"""
        CREATE OR REPLACE TABLE occ_hour AS
        SELECT o.*, o.date + INTERVAL (o.hour) HOUR AS ts,
               c.giants_home, c.n_games, c.first_pitch_hour, c.day_night,
               c.clean_control, c.dow
        FROM '{silver.rstrip('/')}/occupancy_ring_hour.parquet' o
        JOIN panel_dates c USING (date)
    """)
    n_dh = con.sql("""
        SELECT COUNT(DISTINCT date) FROM occ_hour
        WHERE giants_home AND n_games > 1""").fetchone()[0]
    note("occupancy_es_doubleheaders_excluded", f"{n_dh} doubleheader days excluded")

    measure_rows = " UNION ALL ".join(
        f"SELECT ring_id, ring, date, hour, ts, '{m}' AS measure, "
        f"{m}::DOUBLE AS value, giants_home, n_games, first_pitch_hour, "
        f"day_night, clean_control, dow FROM occ_hour"
        for m in OCC_MEASURES)
    con.sql(f"CREATE OR REPLACE TABLE occ_long AS {measure_rows}")

    # Night games: relative_hour +6..+8 lands at 01:00-03:00 the next calendar
    # date. Those rows exist in occ_long under the NEXT date; pull them by
    # timestamp against the game's first pitch, never by (date, hour) math.
    con.sql(f"""
        CREATE OR REPLACE TABLE occ_game_window AS
        WITH pitches AS (
            SELECT DISTINCT date AS game_date, dow, day_night,
                   date + INTERVAL (first_pitch_hour) HOUR AS pitch_ts
            FROM occ_hour
            WHERE giants_home AND n_games = 1 AND first_pitch_hour IS NOT NULL)
        SELECT l.ring_id, l.ring, l.measure, p.game_date, p.dow, p.day_night,
               l.ts, l.hour, l.value,
               CAST(date_diff('hour', p.pitch_ts, l.ts) AS INT) AS relative_hour
        FROM occ_long l
        JOIN pitches p
          ON l.ts BETWEEN p.pitch_ts + INTERVAL ({REL_HOUR_MIN}) HOUR
                      AND p.pitch_ts + INTERVAL ({REL_HOUR_MAX}) HOUR
    """)

    # Matched-control baseline per (game, ring, measure, relative_hour):
    # nearest MATCH_K clean-control ANCHOR days (same dow as the game date),
    # each read at the treated hour's offset from the game date's midnight.
    # Offsets past 24h land on the day AFTER the anchor, the night the anchor
    # produced (docstring trap 2). Anchor cleanliness mirrors the treated
    # side, where only the GAME date is conditioned on, not the next morning.
    con.sql(f"""
        CREATE OR REPLACE TABLE occ_baseline AS
        SELECT game_date, ring_id, measure, relative_hour,
               AVG(cv) AS baseline, STDDEV_SAMP(cv) AS baseline_sd,
               COUNT(cv) AS baseline_n
        FROM (
            SELECT g.game_date, g.ring_id, g.measure, g.relative_hour,
                   c.value AS cv,
                   row_number() OVER (
                       PARTITION BY g.game_date, g.ring_id, g.measure,
                                    g.relative_hour
                       ORDER BY abs(date_diff('day', g.game_date, a.date)), a.date) AS rk
            FROM occ_game_window g
            JOIN panel_dates a
              ON a.clean_control AND a.dow = g.dow
             AND abs(date_diff('day', g.game_date, a.date))
                 <= {MATCH_MAX_WINDOW_DAYS}
            JOIN occ_long c
              ON c.ring_id = g.ring_id AND c.measure = g.measure
             AND c.ts = a.date::TIMESTAMP + (g.ts - g.game_date::TIMESTAMP)
             AND c.value IS NOT NULL)
        WHERE rk <= {MATCH_K}
        GROUP BY 1, 2, 3, 4
    """)

    con.sql(f"""
        CREATE OR REPLACE TABLE occupancy_event_study AS
        WITH per_game AS (
            SELECT g.ring_id, g.ring, g.measure, g.relative_hour, g.day_night,
                   g.value - b.baseline AS lift
            FROM occ_game_window g
            JOIN occ_baseline b
              ON b.game_date = g.game_date AND b.ring_id = g.ring_id
             AND b.measure = g.measure AND b.relative_hour = g.relative_hour
            WHERE b.baseline_n >= {MIN_CONTROLS}),
        sliced AS (
            SELECT ring_id, ring, measure, relative_hour, 'all' AS slice, lift
            FROM per_game
            UNION ALL
            SELECT ring_id, ring, measure, relative_hour, day_night, lift
            FROM per_game WHERE day_night IS NOT NULL)
        SELECT ring_id, ring, measure, relative_hour, slice,
               AVG(lift) AS effect,
               STDDEV_SAMP(lift) / sqrt(COUNT(*)) AS se,
               AVG(lift) / NULLIF(STDDEV_SAMP(lift) / sqrt(COUNT(*)), 0) AS t,
               COUNT(*) AS n_games
        FROM sliced
        GROUP BY 1, 2, 3, 4, 5
    """)

    thin = con.sql(f"""
        SELECT COUNT(*) FROM (
            SELECT g.game_date, g.ring_id, g.measure, g.relative_hour
            FROM occ_game_window g
            LEFT JOIN occ_baseline b
              ON b.game_date = g.game_date AND b.ring_id = g.ring_id
             AND b.measure = g.measure AND b.relative_hour = g.relative_hour
            WHERE COALESCE(b.baseline_n, 0) < {MIN_CONTROLS}
            GROUP BY 1, 2, 3, 4)
    """).fetchone()[0]
    gate("occupancy_es_coverage", thin == 0,
         f"{thin} game-ring-measure-hour cells with < {MIN_CONTROLS} controls")

    peak = con.sql("""
        SELECT relative_hour, ROUND(effect, 1) FROM occupancy_event_study
        WHERE ring_id = 1 AND measure = 'visitor_hours' AND slice = 'all'
        ORDER BY effect DESC LIMIT 1""").fetchone()
    if peak is None:
        expect("occupancy_es_peak_at_zero", False,
               "no ring-1 visitor_hours slice=all rows in occupancy_event_study")
    else:
        peak_rel, peak_eff = peak
        expect("occupancy_es_peak_at_zero", -1 <= peak_rel <= 2,
               f"ring-1 visitor_hours peak effect {peak_eff} at relative_hour "
               f"{peak_rel}, want in [-1, +2]")

    outer_t = con.sql("""
        SELECT AVG(effect / NULLIF(se, 0)) FROM occupancy_event_study
        WHERE ring_id = 5 AND measure = 'visitor_hours' AND slice = 'all'
    """).fetchone()[0]
    outer_txt = "no rows" if outer_t is None else f"{outer_t:.2f}"
    expect("occupancy_es_outer_null", outer_t is not None and outer_t < 2.0,
           f"ring-5 mean t = {outer_txt}, placebo ring expected null (< 2)")
    n = con.sql("SELECT COUNT(*) FROM occupancy_event_study").fetchone()[0]
    log(f"occupancy_event_study: {n} rows")

    # REPORT-ONLY control-pool sensitivity: clean_control keeps Chase/Moscone
    # days in the pool by design (they are covariates). If those events
    # inflate control baselines, the lift is understated. Recompute the ring-1
    # visitor_hours curve with a STRICT pool (also excluding chase/moscone
    # days) and report the peak-effect change. Not a gate: a big delta is a
    # team conversation about the estimator, not a broken build.
    strict = con.sql(f"""
        WITH strict_base AS (
            SELECT game_date, ring_id, measure, relative_hour,
                   AVG(cv) AS baseline, COUNT(cv) AS n
            FROM (
                SELECT g.game_date, g.ring_id, g.measure, g.relative_hour,
                       c.value AS cv,
                       row_number() OVER (
                           PARTITION BY g.game_date, g.ring_id, g.measure,
                                        g.relative_hour
                           ORDER BY abs(date_diff('day', g.game_date, a.date)), a.date) AS rk
                FROM occ_game_window g
                JOIN panel_dates a
                  ON a.clean_control AND a.dow = g.dow
                 AND abs(date_diff('day', g.game_date, a.date))
                     <= {MATCH_MAX_WINDOW_DAYS}
                 AND a.date NOT IN (
                     SELECT date FROM panel
                     WHERE chase_event IS NOT NULL
                        OR moscone_event IS NOT NULL)
                JOIN occ_long c
                  ON c.ring_id = g.ring_id AND c.measure = g.measure
                 AND c.ts = a.date::TIMESTAMP + (g.ts - g.game_date::TIMESTAMP)
                 AND c.value IS NOT NULL
                WHERE g.ring_id = 1 AND g.measure = 'visitor_hours')
            WHERE rk <= {MATCH_K}
            GROUP BY 1, 2, 3, 4)
        SELECT s.relative_hour,
               AVG(g.value - s.baseline) AS strict_effect
        FROM occ_game_window g
        JOIN strict_base s
          ON s.game_date = g.game_date AND s.ring_id = g.ring_id
         AND s.measure = g.measure AND s.relative_hour = g.relative_hour
        WHERE s.n >= {MIN_CONTROLS}
        GROUP BY 1
    """).fetchall()
    strict_d = dict(strict)
    peak_default, peak_rel2 = con.sql("""
        SELECT effect, relative_hour FROM occupancy_event_study
        WHERE ring_id = 1 AND measure = 'visitor_hours' AND slice = 'all'
        ORDER BY effect DESC LIMIT 1""").fetchone()
    peak_strict = strict_d.get(peak_rel2)
    if peak_strict is not None and peak_default:
        delta_pct = 100.0 * (peak_strict - peak_default) / peak_default
        note("control_pool_sensitivity",
             f"ring-1 peak effect {peak_default:.0f} (default pool) vs "
             f"{peak_strict:.0f} (strict, chase/moscone excluded): {delta_pct:+.1f}%"
             + ("; over 10%, discuss the control pool with the team"
                if abs(delta_pct) > 10 else ""))
    else:
        skipped("control_pool_sensitivity",
                "strict pool too thin at the peak hour to compare")


def build_dollars(con):
    """Step 4 [STUB]: dollars_per_visit + game_impact_dollars.

    Planned math (team decision 2026-07-18 pending):
      Primary: Luke's daily CDTFA food-services disaggregation (currently
      eia-nowcast/gold/sf_food_services_daily_2024, extended to all years once
      his code is in the repo) divided by SF-wide daily visits_food gives
      $/visit by period, with the disaggregation SEs carried through.
      Cross-check: the Economic Census + FRED $/visit factor table
      (bronze/derived_spend_inputs/).
      Then: game_impact_dollars = ring visits_food lift x $/visit, summed
      over rings, with uncertainty from both the lift SE and the $/visit SE.
    Blocked on: Luke's disaggregation code covering 2022-2026 (his current
    output is 2024 only)."""
    note("dollars_built", "stub: not yet implemented (blocked on multi-year "
                          "disaggregation from Luke's pipeline)")


def build_gnn_tables(con, silver, bronze_advan):
    """GNN data contract (design doc section 8b): nodes, edges, time spine,
    sparse targets, coverage mask, and game labels for the two-part model
    (counterfactual forecaster + generalization head).

    The ONE place gold reads bronze: catchment edges need VISITOR_HOME_CBGS
    vectors, which no silver table carries. Everything else comes from silver.
    """
    s = silver.rstrip("/")

    # --- nodes: POIs inside 2.5 km (rings 1..GNN_MAX_RING) ------------------
    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_nodes AS
        SELECT p.FOOTPRINT_ID AS footprint_id, p.ring_id, p.ring,
               p.dist_m, p.lat, p.lon, p.naics_code, p.top_category,
               p.location_name = '{VENUE_POI_NAME}' AS is_venue,
               COALESCE(b.n_covered, 0) AS weeks_covered,
               b.n_weeks IS NOT NULL AND b.n_covered = b.n_weeks
                   AS hourly_complete
        FROM '{s}/poi_rings.parquet' p
        LEFT JOIN (
            SELECT footprint_id, COUNT(*) AS n_weeks,
                   COUNT(*) FILTER (has_hourly) AS n_covered
            FROM '{s}/occupancy_poi_week_coverage.parquet'
            GROUP BY 1) b ON b.footprint_id = p.FOOTPRINT_ID
        WHERE p.ring_id IS NOT NULL AND p.ring_id <= {GNN_MAX_RING}
    """)
    n_nodes = con.sql("SELECT COUNT(*) FROM gnn_nodes").fetchone()[0]
    ring_ref = con.sql(f"""
        SELECT COUNT(*) FROM '{s}/poi_rings.parquet'
        WHERE ring_id IS NOT NULL AND ring_id <= {GNN_MAX_RING}""").fetchone()[0]
    gate("gnn_nodes_match_rings", n_nodes == ring_ref,
         f"{n_nodes} nodes vs {ring_ref} POIs in rings 1-{GNN_MAX_RING}")

    # --- spatial edges: symmetric k-NN by haversine -------------------------
    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_edges_spatial AS
        WITH pairs AS (
            SELECT a.footprint_id AS src, b.footprint_id AS dst,
                   2 * 6371008.8 * asin(sqrt(
                       pow(sin(radians(b.lat - a.lat) / 2), 2)
                       + cos(radians(a.lat)) * cos(radians(b.lat))
                         * pow(sin(radians(b.lon - a.lon) / 2), 2)))
                       AS edge_dist_m
            FROM gnn_nodes a JOIN gnn_nodes b
              ON a.footprint_id != b.footprint_id),
        knn AS (
            SELECT src, dst, edge_dist_m,
                   row_number() OVER (PARTITION BY src
                                      ORDER BY edge_dist_m, dst) AS rk
            FROM pairs)
        SELECT LEAST(src, dst) AS a, GREATEST(src, dst) AS b,
               MIN(edge_dist_m) AS edge_dist_m
        FROM knn WHERE rk <= {K_SPATIAL}
        GROUP BY 1, 2
    """)
    # store undirected once (a < b); loaders emit both directions
    n_sp, selfloops = con.sql("""
        SELECT COUNT(*), COUNT(*) FILTER (a = b) FROM gnn_edges_spatial
    """).fetchone()
    deg_min, deg_max = con.sql("""
        WITH deg AS (
            SELECT n.footprint_id, COUNT(e.a) AS d
            FROM gnn_nodes n
            LEFT JOIN (SELECT a FROM gnn_edges_spatial
                       UNION ALL SELECT b FROM gnn_edges_spatial) e(a)
              ON e.a = n.footprint_id
            GROUP BY 1)
        SELECT MIN(d), MAX(d) FROM deg""").fetchone()
    gate("gnn_edges_valid",
         selfloops == 0 and deg_min >= K_SPATIAL,
         f"{n_sp} undirected spatial edges, {selfloops} self-loops, "
         f"degree range [{deg_min}, {deg_max}] (floor {K_SPATIAL})")

    # --- catchment edges: cosine of VISITOR_HOME_CBGS vectors ---------------
    # Advan's trade-area guidance: treat these columns as ratios. Cosine is a
    # ratio-style use. Vectors aggregate the whole occupancy window; nodes
    # without vectors (privacy-floored or unreported) get no catchment edges
    # and stay connected via spatial k-NN.
    con.sql(f"""
        CREATE OR REPLACE TABLE cbg_vec AS
        SELECT n.footprint_id, k.cbg,
               SUM(TRY_CAST(json_extract_string(
                   a.VISITOR_HOME_CBGS, '$."' || k.cbg || '"') AS DOUBLE)) AS w
        FROM read_parquet('{bronze_advan}') a
        JOIN gnn_nodes n ON n.footprint_id = a.FOOTPRINT_ID,
             UNNEST(json_keys(a.VISITOR_HOME_CBGS)) AS k(cbg)
        WHERE a.VISITOR_HOME_CBGS IS NOT NULL
          AND a.DATE_RANGE_START::DATE >= DATE '{SPLITS["train"][0]}'
          AND a.DATE_RANGE_START::DATE <= DATE '{SPLITS["test"][1]}'
        GROUP BY 1, 2
    """)
    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_edges_catchment AS
        WITH norms AS (
            SELECT footprint_id, sqrt(SUM(w * w)) AS nrm
            FROM cbg_vec GROUP BY 1),
        dots AS (
            SELECT x.footprint_id AS src, y.footprint_id AS dst,
                   SUM(x.w * y.w) AS dot
            FROM cbg_vec x JOIN cbg_vec y
              ON x.cbg = y.cbg AND x.footprint_id < y.footprint_id
            GROUP BY 1, 2),
        cos AS (
            SELECT d.src, d.dst, d.dot / (na.nrm * nb.nrm) AS cos_sim
            FROM dots d
            JOIN norms na ON na.footprint_id = d.src
            JOIN norms nb ON nb.footprint_id = d.dst
            WHERE d.dot / (na.nrm * nb.nrm) >= {MIN_COS}),
        ranked AS (
            SELECT src, dst, cos_sim, row_number() OVER (
                       PARTITION BY src ORDER BY cos_sim DESC, dst) AS rk_s,
                   row_number() OVER (
                       PARTITION BY dst ORDER BY cos_sim DESC, src) AS rk_d
            FROM cos)
        SELECT src AS a, dst AS b, cos_sim
        FROM ranked WHERE rk_s <= {K_CATCH} OR rk_d <= {K_CATCH}
    """)
    n_cat, cat_nodes = con.sql("""
        SELECT COUNT(*),
               (SELECT COUNT(DISTINCT f) FROM (
                   SELECT a AS f FROM gnn_edges_catchment
                   UNION SELECT b FROM gnn_edges_catchment))
        FROM gnn_edges_catchment""").fetchone()
    share = cat_nodes / n_nodes
    gate("gnn_catchment_coverage", share >= 0.5,
         f"{n_cat} catchment edges touch {cat_nodes}/{n_nodes} nodes "
         f"({share:.0%}), want >= 50%")

    # --- hourly time spine with covariates and splits ------------------------
    # Covariate additions (2026-07-28): full daily weather (tmin/tavg/awnd),
    # the holiday flag, HOURLY weather (silver weather_hour: SFO LCD, F/in/mph),
    # and HOURLY Chase windows (silver event_hour: real tip-offs from the ESPN
    # re-pull that also fixed the UTC +1-day date bug; concerts use the
    # documented 19-23 default). Day flags stay for day-grain consumers.
    split_case = " ".join(
        f"WHEN date BETWEEN DATE '{a}' AND DATE '{b}' THEN '{k}'"
        for k, (a, b) in SPLITS.items())
    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_time_hour AS
        SELECT d.date, h.hour::SMALLINT AS hour,
               d.date + INTERVAL (h.hour) HOUR AS ts,
               d.giants_home, d.n_games, d.first_pitch_hour, d.day_night,
               d.clean_control, d.dow, month(d.date) AS month,
               CASE WHEN d.giants_home AND d.n_games = 1
                         AND d.first_pitch_hour IS NOT NULL
                    THEN CAST(date_diff('hour',
                         d.date + INTERVAL (d.first_pitch_hour) HOUR,
                         d.date + INTERVAL (h.hour) HOUR) AS INT)
               END AS relative_hour,
               p.ballpark_event IS NOT NULL AS ballpark_day,
               p.chase_event IS NOT NULL AS chase_day,
               p.moscone_event IS NOT NULL AS moscone_day,
               p.citywide_event IS NOT NULL AS citywide_day,
               p.street_fair IS NOT NULL AS street_fair_day,
               p.us_federal_holiday,
               p.tmax, p.tmin, p.tavg, p.prcp, p.awnd,
               w.temp_hr, w.prcp_hr, w.wind_hr,
               COALESCE(e.chase_event_hour, FALSE) AS chase_event_hour,
               CASE {split_case} END AS split
        FROM panel_dates d
        JOIN (SELECT DISTINCT date, ballpark_event, chase_event,
                     moscone_event, citywide_event, street_fair,
                     us_federal_holiday, tmax, tmin, tavg, prcp, awnd
              FROM panel) p USING (date)
        CROSS JOIN (SELECT UNNEST(generate_series(0, 23)) AS hour) h
        LEFT JOIN '{s}/weather_hour.parquet' w
               ON w.date = d.date AND w.hour = h.hour
        LEFT JOIN '{s}/event_hour.parquet' e
               ON e.date = d.date AND e.hour = h.hour
        WHERE d.date BETWEEN DATE '{SPLITS["train"][0]}'
                         AND DATE '{SPLITS["test"][1]}'
    """)
    bad_split, n_hours = con.sql("""
        SELECT COUNT(*) FILTER (split IS NULL), COUNT(*)
        FROM gnn_time_hour""").fetchone()
    gate("gnn_splits_partition", bad_split == 0,
         f"{n_hours} spine hours, {bad_split} outside any split")
    leak = count_split_boundary_leaks(con, "gnn_time_hour")
    gate("gnn_no_leakage", leak == 0,
         f"{leak} game hours carry a split label their date does not fall in")
    wx_cov = con.sql("""
        SELECT AVG((temp_hr IS NOT NULL)::INT) FROM gnn_time_hour
    """).fetchone()[0]
    gate("gnn_weather_coverage", wx_cov is not None and wx_cov >= 0.95,
         f"{wx_cov:.1%} of spine hours carry hourly temp")
    ch = con.sql("""
        SELECT COUNT(*) FROM gnn_time_hour
        WHERE chase_event_hour AND NOT chase_day""").fetchone()[0]
    gate("gnn_event_hour_within_day", ch == 0,
         f"{ch} chase_event_hour rows outside a chase_day")

    # --- sparse targets + coverage mask, node set only -----------------------
    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_target_node_hour AS
        SELECT o.footprint_id, o.date, o.hour, o.visitor_hours
        FROM '{s}/occupancy_poi_hour.parquet' o
        JOIN gnn_nodes n USING (footprint_id)
    """)
    n_tgt = con.sql("SELECT COUNT(*) FROM gnn_target_node_hour").fetchone()[0]
    ref = con.sql(f"""
        SELECT COUNT(*) FROM '{s}/occupancy_poi_hour.parquet' o
        JOIN '{s}/poi_rings.parquet' p ON p.FOOTPRINT_ID = o.footprint_id
        WHERE p.ring_id <= {GNN_MAX_RING}""").fetchone()[0]
    gate("gnn_target_matches_silver", n_tgt == ref,
         f"{n_tgt:,} target rows vs {ref:,} silver POI-hours in rings "
         f"1-{GNN_MAX_RING}")

    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_node_week_coverage AS
        SELECT c.footprint_id, c.week_start, c.has_hourly
        FROM '{s}/occupancy_poi_week_coverage.parquet' c
        JOIN gnn_nodes n USING (footprint_id)
    """)

    # --- generalization-head labels: v0 lifts on occupancy-window games ------
    con.sql(f"""
        CREATE OR REPLACE TABLE gnn_game_labels AS
        SELECT date, ring_id, ring, measure, observed, baseline, lift,
               lift_pct, attendance, day_night, first_pitch_hour, dow
        FROM game_effects
        WHERE date >= DATE '{SPLITS["train"][0]}'
    """)
    n_lab = con.sql("SELECT COUNT(*) FROM gnn_game_labels").fetchone()[0]
    log(f"gnn tables: {n_nodes} nodes, {n_sp} spatial + {n_cat} catchment "
        f"edges, {n_hours} spine hours, {n_tgt:,} targets, {n_lab} labels")


def build_impact_model(con):
    """Step 5 [STUB]: the generalizable impact function.

    Team decision 2026-07-28: the model is a GNN, in two parts. (1) A
    counterfactual forecaster trained on clean non-game node-hours to predict
    visitor_hours per POI-hour (lift = observed - prediction on game hours);
    this is the "regression baseline v1" and must beat the matched-control v0.
    (2) A generalization head mapping game covariates to per-ring lift for the
    "project any future event" deliverable, labels from gnn_game_labels.
    The full data contract (nodes, spatial + catchment edges, hourly spine
    with splits, sparse targets, coverage mask) is built by build_gnn_tables();
    see docs/design/2026-07-28-advan-occupancy.md section 8b. Training runs
    downstream (SageMaker; quota wall cleared 2026-07-28), registered in the
    Model Registry per the existing promotion gate.
    Note for the modeling: at ring 1, daily lift is nearly flat in attendance
    (corr ~0.13); check whether attendance matters more at outer rings
    before committing to it as the headline input."""
    note("impact_model_built", "stub: GNN data contract built by "
                               "build_gnn_tables; training downstream")


MIN_CROSSCHECK_CORR = 0.90  # ring 1 (0-250m) is a subset of his 0-300m POI set


def crosscheck_luke(con, residuals):
    """Our ring-1 visits_food lift vs Luke's (v - exp) gap on the same game days.

    His exp is a same-dow control mean with a different window, and since the
    2026-07-18 metric re-ring our ring 1 (0-250m) is a subset of his 0-300m POI
    set, so we expect high corr, not equality.

    Never aborts the build: `residuals` is an artifact of the other pipeline and
    may legitimately be unreachable. But it is never recorded as agreement it did
    not establish, so a moved prefix cannot quietly retire the only check that
    ties the two pipelines together.
    """
    try:
        corr, n = con.sql(f"""
            SELECT corr(g.lift, l.v - l.exp), COUNT(*)
            FROM game_effects g
            JOIN '{residuals}' l ON l.date = g.date
            WHERE g.ring_id = 1 AND g.measure = 'visits_food'
        """).fetchone()
    except Exception as e:
        skipped("crosscheck_luke_gap", f"{residuals} unreadable: {e}")
        return
    if not n or corr is None:
        skipped("crosscheck_luke_gap",
                f"no overlapping game days with {residuals} (n={n})")
        return
    crosscheck("crosscheck_luke_gap", corr >= MIN_CROSSCHECK_CORR,
               f"n={n}, corr(our lift, his v-exp)={corr:.4f}, "
               f"want >= {MIN_CROSSCHECK_CORR}")


TABLES = ["game_effects", "event_study_ring", "distance_decay",
          "occupancy_event_study", "gnn_nodes", "gnn_edges_spatial",
          "gnn_edges_catchment", "gnn_time_hour", "gnn_target_node_hour",
          "gnn_node_week_coverage", "gnn_game_labels"]


def write_outputs(con, out, silver, started):
    os.makedirs(out, exist_ok=True)
    counts = {}
    for t in TABLES:
        con.sql(f"COPY (SELECT * FROM {t}) TO '{os.path.join(out, t)}.parquet' (FORMAT parquet)")
        counts[t] = con.sql(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    manifest = {
        "built_at": started.isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "silver": silver,
        "params": {"measures": MEASURES, "match_k": MATCH_K,
                   "match_max_window_days": MATCH_MAX_WINDOW_DAYS,
                   "min_controls": MIN_CONTROLS, "ring_mid_m": RING_MID_M,
                   "occ_measures": OCC_MEASURES,
                   "rel_hour_range": [REL_HOUR_MIN, REL_HOUR_MAX],
                   "gnn_max_ring": GNN_MAX_RING, "k_spatial": K_SPATIAL,
                   "k_catch": K_CATCH, "min_cos": MIN_COS, "splits": SPLITS},
        "tables": counts,
        "stubs": ["dollars_per_visit", "game_impact_dollars", "impact_model"],
        "qa": QA,
    }
    with open(os.path.join(out, "build_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    with open(os.path.join(out, "README.md"), "w") as f:
        f.write(readme_text(manifest))
    log(f"wrote {len(TABLES)} tables + build_manifest.json + README.md to {out}")


def qa_headline(qa):
    """One line for the top of the README: did anything need a human to look?"""
    unmet = [k for k, v in qa.items()
             if v.get("kind") in ("expectation", "crosscheck", "skipped")
             and not v.get("ok")]
    if not unmet:
        return ("All integrity gates passed, every result expectation was met, and "
                "every crosscheck ran and agreed.")
    n = len(unmet)
    return (f"All integrity gates passed (the build would have aborted otherwise), "
            f"but {n} soft check{'' if n == 1 else 's'} "
            f"{'wants' if n == 1 else 'want'} a human: "
            f"{', '.join(f'`{k}`' for k in unmet)}. See the QA log below.")


def readme_text(m):
    rows = "\n".join(f"| `{t}.parquet` | {n:,} |" for t, n in m["tables"].items())
    qa = qa_markdown(m["qa"])
    return f"""# gold/ - model-ready answers (SCAFFOLD build)

DERIVED. Built only by `pipeline/build_gold.py` from silver; never
hand-edited. Publishing to the bucket's `gold/` prefix is gated on team
promotion; agent-loop experiment variants go to `experiments/<run-id>/`.

Baseline estimator v0: matched clean-control mean (nearest
{m['params']['match_k']} same day-of-week clean days, max
+/- {m['params']['match_max_window_days']} days). The GNN counterfactual
forecaster is the planned v1 (see the gnn_* data contract below).

| Table | Rows |
|---|---|
{rows}

`occupancy_event_study` and every `gnn_*` table use the OCCUPANCY window
(2023+, silver's OCC_START/OCC_END; Advan changed the hourly construction at
2023 Q1, so 2022 visitor-hours are excluded). `game_effects`,
`event_study_ring`, and `distance_decay` keep the full 2022+ daily window.
visitor_hours is presence per hourly bucket, NOT a headcount; never mix it
with visits. GNN contract: nodes = rings 1-{m['params']['gnn_max_ring']}
POIs; edges = spatial {m['params']['k_spatial']}-NN + VISITOR_HOME_CBGS
cosine (>= {m['params']['min_cos']}, top {m['params']['k_catch']}); temporal
splits {m['params']['splits']}; sparse targets disambiguated by
gnn_node_week_coverage (missing + covered week = true zero, else unknown).

Stubs (documented in the script, not yet built): {', '.join(m['stubs'])}.

## This build

- built_at: {m['built_at']}
- git_sha: {m['git_sha']}
- silver: {m['silver']}

{qa_headline(m['qa'])}

## QA log

Two classes, deliberately separated. **Integrity gates** are claims about whether
the data was read and assembled correctly, and a failure aborts the build.
**Result expectations** are claims about what the data shows; they are recorded
and never abort, because gating on a finding means a null or contrary result
crashes the build and any estimator change that shrinks the effect looks like
broken plumbing. A check that DID NOT RUN is listed as such and never counts as
agreement.

{qa}
"""


def main():
    ap = argparse.ArgumentParser(description="Build the gold layer from silver")
    ap.add_argument("--silver", default="s3://aai-590-group2-capstone/silver")
    ap.add_argument("--out", default="./gold")
    ap.add_argument("--residuals",
                    default="s3://aai-590-group2-capstone/eia-nowcast/gold/game_residuals_0_300m.parquet")
    # The ONE bronze input gold takes: VISITOR_HOME_CBGS vectors for the GNN
    # catchment edges live only in raw Advan (no silver table carries them).
    ap.add_argument("--bronze-advan",
                    default="s3://aai-590-group2-capstone/bronze/advan_weekly_patterns/*.parquet")
    args = ap.parse_args()

    started = datetime.datetime.now()
    con = duckdb.connect()
    if "s3://" in args.silver + args.residuals + args.bronze_advan:
        con.sql("CREATE OR REPLACE SECRET aws (TYPE s3, PROVIDER credential_chain, "
                "REGION 'us-east-2')")

    log(f"silver={args.silver}  out={args.out}")
    load_panel(con, args.silver)
    build_game_effects(con)
    build_event_study(con)
    build_distance_decay(con)
    build_occupancy_event_study(con, args.silver)
    build_gnn_tables(con, args.silver, args.bronze_advan)
    build_dollars(con)
    build_impact_model(con)
    crosscheck_luke(con, args.residuals)
    write_outputs(con, args.out, args.silver, started)
    log(f"done in {(datetime.datetime.now() - started).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
