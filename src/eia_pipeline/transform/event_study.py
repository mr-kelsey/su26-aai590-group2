"""Event-day arrival uplift for a transit station (Task 01 core, BART/Oracle path).

The question: do BART arrivals at a venue's nearest station rise on home-game days,
above the same-day-of-week baseline, in the pre-game window?

Honest framing (see CLAUDE.md 'presence != people'):
  - EMBR (Embarcadero) is Oracle Park's nearest BART, but a *transfer* mode in a dense
    downtown — arrivals are commute-dominated. So we do NOT compare raw levels; we
    difference each game against the mean of NON-game days sharing its day-of-week,
    over the SAME clock hours, then sum the game's pre-game window.
  - The result is *net BART arrivals in the pre-game window attributable to the game* —
    a partial proxy, a fraction of the ~40k gate, not gross attendance.
"""
from __future__ import annotations

import polars as pl


def station_hourly_arrivals(od: pl.DataFrame, station: str) -> pl.DataFrame:
    """Collapse OD to hourly arrivals at one station: date, hour, dow, arrivals."""
    return (
        od.filter(pl.col("destination") == station)
        .group_by("date", "hour")
        .agg(pl.col("trip_count").sum().alias("arrivals"))
        .with_columns(pl.col("date").dt.weekday().alias("dow"))
    )


def bart_arrival_uplift(
    od: pl.DataFrame,
    schedule: pl.DataFrame,
    station: str,
    window_pre: int = 2,   # include hours [first_pitch-window_pre, first_pitch]
    night_only: bool = True,
) -> tuple[pl.DataFrame, dict]:
    """Return (per-game uplift table, summary dict).

    Baseline = mean arrivals on NON-game days, per (dow, hour). For each game we sum
    actual game-day arrivals over its pre-game window and subtract the matched baseline
    sum. `uplift` = game - baseline for that window.
    """
    hourly = station_hourly_arrivals(od, station)

    games = schedule.filter(pl.col("day_night") == "night") if night_only else schedule
    game_dates = set(games["game_date"].to_list())

    # Baseline per (dow, hour) from non-game days only (avoid leaking event signal in).
    baseline = (
        hourly.filter(~pl.col("date").is_in(list(game_dates)))
        .group_by("dow", "hour")
        .agg(
            pl.col("arrivals").mean().alias("base_mean"),
            pl.col("arrivals").std().alias("base_std"),
            pl.len().alias("base_n"),
        )
    )
    base_lookup = {(r["dow"], r["hour"]): r["base_mean"] for r in baseline.to_dicts()}

    # Actual arrivals keyed by (date, hour) for fast window sums.
    actual = {(r["date"], r["hour"]): r["arrivals"] for r in hourly.to_dicts()}

    rows = []
    for g in games.to_dicts():
        d, fp = g["game_date"], g["first_pitch_hour"]
        dow = d.weekday() + 1  # polars dow is 1=Mon..7=Sun; python weekday() 0=Mon
        window = range(fp - window_pre, fp + 1)
        game_sum = sum(actual.get((d, h), 0) for h in window)
        base_sum = sum(base_lookup.get((dow, h), 0.0) for h in window)
        rows.append(
            {
                "game_date": d,
                "opponent": g["opponent"],
                "first_pitch_hour": fp,
                "window": f"{fp - window_pre}-{fp}h",
                "game_arrivals": game_sum,
                "baseline_arrivals": round(base_sum, 1),
                "uplift": round(game_sum - base_sum, 1),
            }
        )

    per_game = pl.DataFrame(rows).sort("game_date")
    up = per_game["uplift"]
    n = len(up)
    mean = up.mean() if n else float("nan")
    std = up.std() if n > 1 else None  # std undefined for n<2
    se = std / (n ** 0.5) if std is not None else None
    summary = {
        "station": station,
        "n_games": n,
        "mean_uplift": round(mean, 1) if n else float("nan"),
        "std_uplift": round(std, 1) if std is not None else None,
        "se": round(se, 1) if se is not None else None,
        "t_stat": round(mean / se, 2) if se else None,
        "pct_games_positive": round(100 * (up > 0).sum() / n, 1) if n else float("nan"),
    }
    return per_game, summary
