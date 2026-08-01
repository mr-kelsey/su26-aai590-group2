"""Phase 4, the dollars bridge: presence effect to per-game taxable food-service dollars.

The chain, and what each link is anchored to:

  1. CDTFA C08 quarterly SF taxable food-service sales     <- the only truth
  2. Denton/Chow-Lin disaggregation onto a daily citywide
     food-visit indicator                                  <- reconciles exactly
  3. Allocate each day's dollars across food POIs by
     visit share, bucketed by distance from home plate     <- assumes uniform $/visit
  4. Apply the measured per-band presence effect to the
     evening share of those dollars                        <- the causal step

Drifting velocity does not break this, which is worth explaining because it looks like it should. Over 2023-2026 our citywide food visits are flat, up 2%, while CDTFA dollars rise 23%. Nearly all of that growth is price rather than volume, and dollars per visit drift up 21%. That would be fatal to a fixed-coefficient velocity model. It is not fatal here, because Denton reconciles every quarter exactly, so cross-quarter drift is absorbed by construction. The method only needs daily dollars to track daily visits within a quarter, which is a much weaker claim. The drift does mean we cannot read velocity as a structural parameter, and we cannot extrapolate beyond the anchor window. This module does neither.

The evening share is not optional. Our effects are estimated on hours 16-23, so they may only be applied to the evening portion of a day's dollars, which we measured at 34.5% of food-POI activity. Applying an evening effect to a full day would inflate the result by roughly 3x.

What this number is not:
  - not retail, lodging, parking, transport, or any in-stadium concession
  - not indirect or induced activity; direct visitor spend only (D7)
  - not total spend; CDTFA measures taxable transactions only

It is therefore a floor on a narrow and well-anchored slice, which is the opposite of how event impact studies usually go wrong.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from ..io import duckdb_s3
from ..settings import settings

VENUE_LAT, VENUE_LON = 37.7786, -122.3893
FOOD_NAICS = "722%"          # CDTFA business group C08
WINDOW = ("2023-01-01", "2025-12-31")

# Measured per-band effects (day-clustered bootstrap, 246 games, improved
# counterfactual). >4km is not distinguishable from zero (p=0.60) and enters as 0
# rather than as its noisy point estimate.
BAND_EFFECTS = {
    "0-500m":   (44.6, 39.8, 49.9),
    "500m-1km": (14.8, 12.2, 17.4),
    "1-2km":    ( 4.5,  3.3,  5.7),
    "2-4km":    ( 1.2,  0.4,  2.0),
    ">4km":     ( 0.0,  0.0,  0.0),
}

BAND_SQL = """CASE WHEN d<=500 THEN '0-500m' WHEN d<=1000 THEN '500m-1km'
                   WHEN d<=2000 THEN '1-2km' WHEN d<=4000 THEN '2-4km'
                   ELSE '>4km' END"""


def evening_share(con=None) -> float:
    """Fraction of food-POI activity falling in hours 16-23."""
    con = con or duckdb_s3()
    return con.execute(f"""
        SELECT sum(CASE WHEN h.hour BETWEEN 16 AND 23 THEN h.person_hours ELSE 0 END)
               / sum(h.person_hours)
        FROM read_parquet('{settings.data_dir}/bronze_sf/hourly/*.parquet') h
        JOIN read_parquet('{settings.data_dir}/bronze_sf/poi_dim.parquet') p
          USING(footprint_id)
        WHERE p.naics LIKE '{FOOD_NAICS}'
          AND h.date BETWEEN DATE '{WINDOW[0]}' AND DATE '{WINDOW[1]}'
    """).fetchone()[0]


def band_day_shares(con=None) -> pl.DataFrame:
    """Each band's share of citywide food-service visits, per day.

    Built over EVERY SF food POI, not the 452-cell modelling substrate, so 100% of
    the anchor's dollars are allocated rather than 81.9% of them.
    """
    con = con or duckdb_s3()
    return con.execute(f"""
        WITH poi AS (
          SELECT footprint_id,
                 sqrt(power((lat-{VENUE_LAT})*111320,2)
                    + power((lon-({VENUE_LON}))*111320*cos(radians({VENUE_LAT})),2)) AS d
          FROM read_parquet('{settings.data_dir}/bronze_sf/poi_dim.parquet')
          WHERE naics LIKE '{FOOD_NAICS}'),
        v AS (
          SELECT f.date, {BAND_SQL} AS band, sum(f.visits) AS visits
          FROM read_parquet('{settings.data_dir}/bronze_sf/daily/*.parquet') f
          JOIN poi p USING(footprint_id)
          WHERE f.date BETWEEN DATE '{WINDOW[0]}' AND DATE '{WINDOW[1]}'
          GROUP BY 1,2)
        SELECT date, band, visits,
               visits / sum(visits) OVER (PARTITION BY date) AS share
        FROM v
    """).pl()


def per_game(con=None, daily_dollars_path: str | None = None) -> tuple[pl.DataFrame, dict]:
    """Per-game incremental taxable food-service dollars, by distance band."""
    con = con or duckdb_s3()
    dd = pl.read_parquet(daily_dollars_path
                         or f"{settings.data_dir}/bronze_sf/sf_food_daily_dollars.parquet")
    ev = evening_share(con)
    bd = band_day_shares(con)
    games = con.execute(f"""
        SELECT DISTINCT date FROM read_parquet('{settings.data_dir}/bronze_sf/model_hour.parquet')
        WHERE giants_home""").pl()

    j = (bd.join(dd.select("date", "value_daily"), on="date")
           .join(games.with_columns(pl.lit(True).alias("game")), on="date", how="left")
           .with_columns(pl.col("game").fill_null(False))
           .filter(pl.col("game")))

    # observed = counterfactual x (1+e)  =>  the game-attributable part of observed
    # activity is e/(1+e), NOT e. Using e directly overstates by (1+e).
    frac = lambda pct: (pct / 100) / (1 + pct / 100)

    rows = []
    for band, (e, lo, hi) in BAND_EFFECTS.items():
        sub = j.filter(pl.col("band") == band)
        if sub.height == 0:
            continue
        evening = (sub["value_daily"] * sub["share"] * ev).to_numpy()
        rows.append({"band": band, "games": sub.height,
                     "evening_dollars": float(evening.mean()),
                     "incremental": float((evening * frac(e)).mean()),
                     "lo": float((evening * frac(lo)).mean()),
                     "hi": float((evening * frac(hi)).mean())})
    r = pl.DataFrame(rows)
    total = {"per_game": float(r["incremental"].sum()),
             "lo": float(r["lo"].sum()), "hi": float(r["hi"].sum()),
             "n_games": int(r["games"].max()), "evening_share": float(ev)}
    total["season"] = total["per_game"] * total["n_games"]
    return r, total
