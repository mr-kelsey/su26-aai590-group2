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
RING_MID_MI = {1: 0.09, 2: 0.34, 3: 0.75, 4: 1.5, 5: 3.5}  # ring midpoints

QA = {}


def log(msg):
    print(f"[build_gold] {msg}", flush=True)


def gate(name, ok, detail):
    QA[name] = {"ok": bool(ok), "detail": detail}
    log(f"QA {'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        sys.exit(f"QA gate failed: {name}: {detail}")


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
                           ORDER BY abs(date_diff('day', g.date, c.date))) AS rk
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
    gate("core_ring_positive_lift", ring1_food and ring1_food > 0,
         f"ring-1 visits_food mean lift = {ring1_food}")


def build_event_study(con):
    """Step 2: pool per-game lifts into per-ring effects, with slices."""
    con.sql("""
        CREATE OR REPLACE TABLE sliced AS
        SELECT *, 'all' AS slice FROM game_effects
        UNION ALL
        SELECT *, day_night AS slice FROM game_effects WHERE day_night IS NOT NULL
        UNION ALL
        SELECT * EXCLUDE (t), 'att_t' || t AS slice FROM (
            SELECT *, ntile(3) OVER (ORDER BY attendance) AS t
            FROM game_effects WHERE attendance IS NOT NULL)
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
    tstat = core[0] / core[1] if core and core[1] else 0
    gate("core_effect_significant", tstat > 4,
         f"ring-1 visits_food: {core[0]} +/- {core[1]} ({core[2]}%), t = {tstat:.1f}")


def build_distance_decay(con):
    """Step 3: pct lift by ring distance + a log-linear decay fit."""
    mid_case = " ".join(f"WHEN {k} THEN {v}" for k, v in RING_MID_MI.items())
    con.sql(f"""
        CREATE OR REPLACE TABLE distance_decay AS
        SELECT ring_id, ring, measure,
               CASE ring_id {mid_case} END AS ring_mid_mi,
               n_games, mean_lift, se_lift, mean_lift_pct, se_lift_pct
        FROM event_study_ring
        WHERE slice = 'all'
        ORDER BY measure, ring_id
    """)
    fits = {}
    for m in MEASURES:
        row = con.sql(f"""
            SELECT regr_slope(ln(mean_lift_pct), ring_mid_mi),
                   regr_intercept(ln(mean_lift_pct), ring_mid_mi),
                   COUNT(*)
            FROM distance_decay
            WHERE measure = '{m}' AND mean_lift_pct > 0
        """).fetchone()
        fits[m] = {"log_slope_per_mi": row[0], "log_intercept": row[1],
                   "rings_used": row[2]}
    QA["decay_fits"] = {"ok": True, "detail": fits}
    inner, outer = con.sql("""
        SELECT MAX(mean_lift_pct) FILTER (ring_id = 1),
               MAX(mean_lift_pct) FILTER (ring_id = 5)
        FROM distance_decay WHERE measure = 'visits_food'
    """).fetchone()
    gate("decay_direction", inner is not None and outer is not None and inner > outer,
         f"visits_food pct lift: ring 1 = {inner}, ring 5 = {outer}")


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
    log("dollars: STUB, skipped (see docstring; blocked on multi-year "
        "disaggregation from Luke's pipeline)")
    QA["dollars_built"] = {"ok": True, "detail": "stub: not yet implemented"}


def build_impact_model(con):
    """Step 5 [STUB]: the generalizable impact function.

    Planned: train on game_effects (covariates: attendance, day_night,
    first_pitch_hour, dow, month, opponent, chase_day/moscone_day; target:
    per-ring lift). Validate across seasons and opponents (single-venue
    scope). Register in the SageMaker Model Registry with a model card;
    approval there is the promotion gate that triggers endpoint deploy
    (reuse of the 540 MLOps stack). Gold stores the training table and eval
    metrics; the model artifact lives in the SageMaker default bucket and is
    referenced from build_manifest.json.
    Note for the modeling: at ring 1, lift is nearly flat in attendance
    (corr ~0.13); check whether attendance matters more at outer rings
    before committing to it as the headline input."""
    log("impact_model: STUB, skipped (see docstring)")
    QA["impact_model_built"] = {"ok": True, "detail": "stub: not yet implemented"}


def crosscheck_luke(con, residuals):
    """Report-only: our ring-1 visits_food lift vs Luke's (v - exp) gap on
    the same game days. His exp is a same-dow control mean with a different
    window, so we expect high corr, not equality."""
    try:
        corr, n = con.sql(f"""
            SELECT corr(g.lift, l.v - l.exp), COUNT(*)
            FROM game_effects g
            JOIN '{residuals}' l ON l.date = g.date
            WHERE g.ring_id = 1 AND g.measure = 'visits_food'
        """).fetchone()
        QA["crosscheck_luke_gap"] = {"ok": True,
                                     "detail": f"n={n}, corr(our lift, his v-exp)={corr:.4f}"}
        log(f"cross-check vs Luke's gap: n={n}, corr={corr:.4f}")
    except Exception as e:
        QA["crosscheck_luke_gap"] = {"ok": True, "detail": f"skipped: {e}"}
        log(f"cross-check vs Luke skipped: {e}")


TABLES = ["game_effects", "event_study_ring", "distance_decay"]


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
                   "min_controls": MIN_CONTROLS, "ring_mid_mi": RING_MID_MI},
        "tables": counts,
        "stubs": ["dollars_per_visit", "game_impact_dollars", "impact_model"],
        "qa": QA,
    }
    with open(os.path.join(out, "build_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    with open(os.path.join(out, "README.md"), "w") as f:
        f.write(readme_text(manifest))
    log(f"wrote {len(TABLES)} tables + build_manifest.json + README.md to {out}")


def readme_text(m):
    rows = "\n".join(f"| `{t}.parquet` | {n:,} |" for t, n in m["tables"].items())
    qa = "\n".join(f"- {'PASS' if v['ok'] else 'FAIL'} `{k}`: {v['detail']}"
                   for k, v in m["qa"].items())
    return f"""# gold/ - model-ready answers (SCAFFOLD build)

DERIVED. Built only by `pipeline/build_gold.py` from silver; never
hand-edited. Publishing to the bucket's `gold/` prefix is gated on team
promotion; agent-loop experiment variants go to `experiments/<run-id>/`.

Baseline estimator v0: matched clean-control mean (nearest
{m['params']['match_k']} same day-of-week clean days, max
+/- {m['params']['match_max_window_days']} days). Regression baseline is the
planned v1.

| Table | Rows |
|---|---|
{rows}

Stubs (documented in the script, not yet built): {', '.join(m['stubs'])}.

## This build

- built_at: {m['built_at']}
- git_sha: {m['git_sha']}
- silver: {m['silver']}

QA:
{qa}
"""


def main():
    ap = argparse.ArgumentParser(description="Build the gold layer from silver")
    ap.add_argument("--silver", default="s3://aai-590-group2-capstone/silver")
    ap.add_argument("--out", default="./gold")
    ap.add_argument("--residuals",
                    default="s3://aai-590-group2-capstone/eia-nowcast/gold/game_residuals_0_300m.parquet")
    args = ap.parse_args()

    started = datetime.datetime.now()
    con = duckdb.connect()
    if "s3://" in args.silver + args.residuals:
        con.sql("CREATE OR REPLACE SECRET aws (TYPE s3, PROVIDER credential_chain, "
                "REGION 'us-east-2')")

    log(f"silver={args.silver}  out={args.out}")
    load_panel(con, args.silver)
    build_game_effects(con)
    build_event_study(con)
    build_distance_decay(con)
    build_dollars(con)
    build_impact_model(con)
    crosscheck_luke(con, args.residuals)
    write_outputs(con, args.out, args.silver, started)
    log(f"done in {(datetime.datetime.now() - started).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
