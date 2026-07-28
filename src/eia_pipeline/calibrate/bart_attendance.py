"""Calibrate BART pre-game arrival uplift against gate attendance (Stage D, first point).

This is the first *velocity*-style calibration for the Oracle Park corpus venue: it
turns a presence proxy (net BART arrivals at EMBR in the pre-game window) into a
capture rate against a trusted count (MLB gate attendance).

Honest framing: EMBR is a transfer mode in a dense downtown, so the capture rate is a
few percent — this quantifies exactly *how partial* the BART proxy is, which is the
number the nowcast needs.
Attendance comes from the MLB Stats API (keyless), joined on local game date.
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import requests

from ..ingest.mlb import GIANTS_TEAM_ID, SCHEDULE_URL


def fetch_attendance(season: int, team_id: int = GIANTS_TEAM_ID) -> pl.DataFrame:
    """Return per-home-game gate attendance: columns game_date (pl.Date), attendance.

    One hydrated schedule call carries attendance for every game (no per-game boxscore).
    """
    r = requests.get(
        SCHEDULE_URL,
        params={
            "sportId": 1,
            "teamId": team_id,
            "startDate": f"{season}-03-01",
            "endDate": f"{season}-11-30",
            "gameType": "R",
            "hydrate": "gameInfo",
        },
        timeout=40,
    )
    r.raise_for_status()
    rows = []
    for day in r.json().get("dates", []):
        for g in day.get("games", []):
            if g["teams"]["home"]["team"]["id"] != team_id:
                continue
            att = g.get("gameInfo", {}).get("attendance")
            if att:
                rows.append({"game_date": dt.date.fromisoformat(day["date"]), "attendance": int(att)})
    return pl.DataFrame(rows, schema={"game_date": pl.Date, "attendance": pl.Int64})


def calibrate_capture(per_game: pl.DataFrame, attendance: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """Join per-game uplift to attendance and fit the capture relationship.

    Returns (joined per-game frame with `capture_rate`, summary dict) where summary has:
      - `n`, `corr` (attendance vs uplift)
      - `slope_per_1000` : marginal net BART arrivals per +1000 attendees (OLS)
      - `intercept`
      - `mean_capture_pct`, `median_capture_pct` : uplift / attendance
    """
    j = (
        per_game.join(attendance, on="game_date", how="inner")
        .filter(pl.col("attendance") > 0)
        .with_columns((pl.col("uplift") / pl.col("attendance")).alias("capture_rate"))
    )
    x = j["attendance"].to_numpy().astype(float)
    y = j["uplift"].to_numpy().astype(float)
    # OLS slope/intercept via least squares (numpy, already a dep via polars/pandas stack)
    import numpy as np

    slope, intercept = np.polyfit(x, y, 1)
    summary = {
        "n": j.height,
        "corr": round(float(np.corrcoef(x, y)[0, 1]), 3),
        "slope_per_1000": round(float(slope) * 1000, 1),
        "intercept": round(float(intercept), 1),
        "mean_capture_pct": round(float(j["capture_rate"].mean()) * 100, 2),
        "median_capture_pct": round(float(j["capture_rate"].median()) * 100, 2),
    }
    return j, summary
