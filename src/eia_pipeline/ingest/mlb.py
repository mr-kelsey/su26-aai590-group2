"""MLB Stats API event-catalog ingester (build-now, keyless).
Source: https://statsapi.mlb.com/api/v1/schedule  (public endpoint, NO key required)

Provides the *treatment* for the nowcast: which days had a home game, and the exact
first-pitch time. BART OD is in LOCAL (Pacific) time, so we convert the API's UTC
gameDate to America/Los_Angeles and expose both the local game date and first-pitch
hour — the alignment key for the event-day analysis.

Corpus venue: San Francisco Giants (teamId=137), Oracle Park.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import polars as pl
import requests

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
GIANTS_TEAM_ID = 137
PACIFIC = ZoneInfo("America/Los_Angeles")


def fetch_home_schedule(
    season: int,
    team_id: int = GIANTS_TEAM_ID,
    game_type: str = "R",  # R=regular season
) -> pl.DataFrame:
    """Return one row per home game for a team+season.

    Contract: columns
        game_date       (pl.Date)   — LOCAL (Pacific) calendar date of first pitch
        first_pitch_hour(Int8)      — LOCAL hour 0-23 of first pitch
        game_pk         (Int64)     — MLB game id (join key for attendance later)
        opponent        (str)
        venue           (str)
        day_night       (str)       — 'day' | 'night' (local first pitch < 17:00 => day)
    Home games only (team is the home side). Native/authoritative; no key.
    """
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": f"{season}-03-01",
        "endDate": f"{season}-11-30",
        "gameType": game_type,
    }
    r = requests.get(SCHEDULE_URL, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()

    rows: list[dict] = []
    for day in payload.get("dates", []):
        for g in day.get("games", []):
            if g["teams"]["home"]["team"]["id"] != team_id:
                continue  # away game — not a treatment day for this venue
            # gameDate is ISO-8601 UTC (e.g. 2024-04-09T01:45:00Z). Convert to Pacific.
            utc = pl.Series([g["gameDate"]]).str.to_datetime("%Y-%m-%dT%H:%M:%SZ", time_zone="UTC")
            local = utc.dt.convert_time_zone("America/Los_Angeles")[0]
            rows.append(
                {
                    "game_date": local.date(),
                    "first_pitch_hour": local.hour,
                    "game_pk": g["gamePk"],
                    "opponent": g["teams"]["away"]["team"]["name"],
                    "venue": g["venue"]["name"],
                    "day_night": "day" if local.hour < 17 else "night",
                }
            )

    return pl.DataFrame(
        rows,
        schema={
            "game_date": pl.Date,
            "first_pitch_hour": pl.Int8,
            "game_pk": pl.Int64,
            "opponent": pl.String,
            "venue": pl.String,
            "day_night": pl.String,
        },
    )
