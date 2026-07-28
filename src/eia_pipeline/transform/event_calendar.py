"""Day-level event calendar for Oracle Park — the control-pool cleaner.

The problem this solves: a Giants "off day" that actually had a nearby crowd draw
(a ballpark concert, a Warriors game, a SoMa street fair) is a CONTAMINATED control
that biases the game-effect estimate. And a game day that ALSO had a nearby fair is a
confounded treatment. This module builds one row per calendar date flagging every
crowd-draw tier, so downstream analysis can pick genuinely clean controls and flag
confounded treatments.

Tiers (by how they enter the model):
  - giants_home        : the TREATMENT (from MLB schedule).
  - ballpark_nonbaseball: concerts/other events AT the park — EXCLUDE from controls
                          (or model as own treatment). From competing_events.
  - chase_event        : Warriors/Valkyries/concerts at Chase (~0.7km) — covariate.
  - moscone_conv       : large citywide conventions — downtown-demand covariate.
  - street_fair        : SoMa/South Beach recurring fairs within ~2km — covariate.

`clean_control` = a non-game day with NO crowd-draw of any tier.

Inputs are the local Gold pulls under data/raw/ (see docs/05). Distances to Oracle
Park come from schemas/venue_crosswalk.csv where a fixed venue applies.
"""
from __future__ import annotations

import math
from pathlib import Path

import polars as pl

from ..settings import settings

ORACLE_PARK = (37.7786, -122.3893)
RAW = settings.data_dir / "raw"
# Curated (non-reproducible) reference data is version-controlled under schemas/;
# the street-fair calendar is hand-verified web research, not an API pull.
SCHEMAS = Path(__file__).resolve().parents[3] / "schemas"


def _dist_km(lat: float, lon: float) -> float:
    p = math.pi / 180
    a = (
        math.sin((lat - ORACLE_PARK[0]) * p / 2) ** 2
        + math.cos(ORACLE_PARK[0] * p) * math.cos(lat * p)
        * math.sin((lon - ORACLE_PARK[1]) * p / 2) ** 2
    )
    return round(2 * 6371 * math.asin(math.sqrt(a)), 2)


def _giants_home() -> pl.DataFrame:
    """Treatment: one row per Giants home game with attendance."""
    return (
        pl.read_csv(RAW / "mlb" / "mlb_giants_home_games.csv")
        .with_columns(pl.col("date").str.to_date())
        .filter(pl.col("status") == "Final")
        .select(
            "date",
            pl.lit(True).alias("giants_home"),
            pl.col("attendance").alias("giants_attendance"),
        )
        .unique("date")
    )


def _ballpark_nonbaseball() -> pl.DataFrame:
    """Non-baseball events AT the park — exclude from the control pool."""
    return (
        pl.read_csv(RAW / "competing_events" / "oracle_park_events.csv")
        .with_columns(pl.col("date").str.to_date())
        .select("date", pl.col("name").alias("ballpark_event"))
        .unique("date")
    )


def _chase_events() -> pl.DataFrame:
    """Warriors + Valkyries home games and Chase concerts (~0.7km covariate)."""
    frames = []
    for f in ("nba_warriors_schedule.csv", "wnba_valkyries_schedule.csv"):
        frames.append(
            pl.read_csv(RAW / "competing_events" / f)
            .filter(pl.col("home_game") == True)  # noqa: E712
            .with_columns(pl.col("date").str.to_date())
            .select("date", pl.col("name").alias("chase_event"))
        )
    concerts = (
        pl.read_csv(RAW / "competing_events" / "setlistfm_concerts.csv")
        .filter(pl.col("venue").str.contains("(?i)chase"))
        .with_columns(pl.col("date").str.to_date())
        .select("date", pl.col("artist").alias("chase_event"))
    )
    frames.append(concerts)
    return pl.concat(frames).unique("date")


def _moscone() -> pl.DataFrame:
    """Large citywide conventions — expand multi-day editions to daily rows."""
    m = pl.read_csv(RAW / "competing_events" / "moscone_citywide_events.csv").with_columns(
        pl.col("start_date").str.to_date(), pl.col("end_date").str.to_date()
    )
    # Expand each multi-day edition to one row per day it runs.
    spans = [
        pl.DataFrame({"date": pl.date_range(r["start_date"], r["end_date"] or r["start_date"], "1d", eager=True)})
        .with_columns(pl.lit(r["event"]).alias("moscone_event"))
        for r in m.iter_rows(named=True)
        if r["start_date"] is not None
    ]
    return pl.concat(spans).unique("date")


def _street_fairs() -> pl.DataFrame:
    """Recurring SoMa/South Beach fairs within ~2km that ACTUALLY happened."""
    # Prefer the version-controlled copy; fall back to the local raw pull.
    path = SCHEMAS / "street_fairs_near_oracle.csv"
    if not path.exists():
        path = RAW / "street_fairs" / "street_fairs_near_oracle.csv"
    return (
        pl.read_csv(path)
        .with_columns(pl.col("date").str.to_date())
        .filter(pl.col("status") == "happened")
        .select("date", pl.col("event_name").alias("street_fair"), pl.col("dist_km").alias("street_fair_km"))
        .unique("date")
    )


def build_calendar(start: str = "2016-01-01", end: str = "2025-12-31") -> pl.DataFrame:
    """One row per date over [start, end] with every crowd-draw flag + clean_control."""
    dates = pl.DataFrame(
        {"date": pl.date_range(pl.lit(start).str.to_date(), pl.lit(end).str.to_date(), "1d", eager=True)}
    )
    cal = (
        dates.join(_giants_home(), on="date", how="left")
        .join(_ballpark_nonbaseball(), on="date", how="left")
        .join(_chase_events(), on="date", how="left")
        .join(_moscone(), on="date", how="left")
        .join(_street_fairs(), on="date", how="left")
        .with_columns(pl.col("giants_home").fill_null(False))
    )
    flag = lambda c: pl.col(c).is_not_null()  # noqa: E731
    cal = cal.with_columns(
        any_confounder=(
            flag("ballpark_event") | flag("chase_event") | flag("moscone_event") | flag("street_fair")
        )
    ).with_columns(
        clean_control=(~pl.col("giants_home") & ~pl.col("any_confounder")),
        confounded_treatment=(pl.col("giants_home") & pl.col("any_confounder")),
    )
    return cal


if __name__ == "__main__":
    cal = build_calendar()
    n = cal.height
    games = cal.filter("giants_home").height
    clean = cal.filter("clean_control").height
    conf_tx = cal.filter("confounded_treatment").height
    nongame = cal.filter(~pl.col("giants_home"))
    dirty_ctrl = nongame.filter("any_confounder").height
    print(f"dates: {n} | giants home games: {games}")
    print(f"clean controls (off day, no crowd draw): {clean}")
    print(f"non-game days RECLASSIFIED as contaminated: {dirty_ctrl} "
          f"({dirty_ctrl / nongame.height * 100:.1f}% of non-game days)")
    print(f"confounded treatments (home game + nearby draw): {conf_tx}")
    print("\nreclassified non-game days by tier:")
    for c in ("ballpark_event", "chase_event", "moscone_event", "street_fair"):
        print(f"  {c:16} {nongame.filter(pl.col(c).is_not_null()).height}")
