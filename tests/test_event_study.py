"""Deterministic unit test for the event-day uplift transform (no network)."""
import datetime as dt

import polars as pl

from eia_pipeline.transform.event_study import bart_arrival_uplift, station_hourly_arrivals


def _od_row(date, hour, origin, dest, n):
    return {"date": date, "hour": hour, "origin": origin, "destination": dest, "trip_count": n}


def test_uplift_is_game_minus_same_dow_baseline():
    # Two Tuesdays: one a game day, one not. Station EMBR, first pitch 18:00, window 16-18h.
    game_day = dt.date(2024, 4, 2)      # Tuesday
    control_day = dt.date(2024, 4, 9)   # Tuesday, no game
    rows = []
    # Baseline (control Tuesday): 100 arrivals each hour 16,17,18.
    for h in (16, 17, 18):
        rows.append(_od_row(control_day, h, "MONT", "EMBR", 100))
    # Game Tuesday: 300 arrivals each hour 16,17,18 (uplift 200/hr -> 600 over window).
    for h in (16, 17, 18):
        rows.append(_od_row(game_day, h, "MONT", "EMBR", 300))
    od = pl.DataFrame(rows, schema={
        "date": pl.Date, "hour": pl.Int8, "origin": pl.String,
        "destination": pl.String, "trip_count": pl.Int32,
    })
    schedule = pl.DataFrame(
        [{"game_date": game_day, "first_pitch_hour": 18, "game_pk": 1,
          "opponent": "Test", "venue": "Oracle Park", "day_night": "night"}],
        schema={"game_date": pl.Date, "first_pitch_hour": pl.Int8, "game_pk": pl.Int64,
                "opponent": pl.String, "venue": pl.String, "day_night": pl.String},
    )

    per_game, summary = bart_arrival_uplift(od, schedule, station="EMBR", window_pre=2)
    assert summary["n_games"] == 1
    # game window sum = 900, baseline = 300 -> uplift 600.
    assert per_game["uplift"][0] == 600.0
    assert summary["pct_games_positive"] == 100.0


def test_station_filter_ignores_other_destinations():
    d = dt.date(2024, 4, 2)
    od = pl.DataFrame(
        [_od_row(d, 18, "MONT", "EMBR", 50), _od_row(d, 18, "MONT", "COLS", 999)],
        schema={"date": pl.Date, "hour": pl.Int8, "origin": pl.String,
                "destination": pl.String, "trip_count": pl.Int32},
    )
    hourly = station_hourly_arrivals(od, "EMBR")
    assert hourly["arrivals"].sum() == 50
