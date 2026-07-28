"""First swing at CROWDS -> DOLLARS: calibrate spending velocity for Oracle Park.

Doctrine (docs/02): daily_spend ~ presence(t) x velocity. We calibrate velocity by
regressing an anchor's spend on presence at the anchor's grain. Here:
  - presence  = Giants home attendance (real crowd counts, MLB Stats API)
  - anchor     = OI Affinity SF-county daily card-spend index (% deviation vs Jan-2020)
The attendance coefficient is velocity IN PERCENT-SPACE (% of county spend per fan).
Multiplying by an absolute $/day base (CDTFA's job) turns it into dollars.

HONESTY (this is a first swing, not a settled number):
  - County-TOTAL spend is a smooth aggregate; one game is a spike on it. The estimate
    is real and significant but still carries confounding (game days co-occurring with
    other busy days beyond the day-of-week/month/year controls). Treat dollar figures as
    an order-of-magnitude envelope, biased high, until: (a) a real CDTFA $ base is wired,
    (b) geography is tightened / category-anchored, (c) events are pooled across venues.
  - Daily OI ends ~2022-06, so calibration uses the 2021+early-2022 slate (COVID era;
    capacity limits actually help identification by varying attendance 3.7k-41k).
"""
from __future__ import annotations

import polars as pl


def calibrate_velocity(
    spend: pl.DataFrame,       # date, spend_idx  (from ingest.oi_tracker)
    attendance: pl.DataFrame,  # game_date, attendance  (from calibrate.bart_attendance)
) -> tuple[object, dict]:
    """Fit spend_idx ~ attendance(per 10k) + calendar controls. Return (model, summary).

    summary: n_days, n_game_days, pp_per_10k (+ se, p, 95% CI), mean_attendance,
    lift_at_mean_pp. Requires statsmodels.
    """
    import statsmodels.formula.api as smf

    g = attendance.rename({"game_date": "date"})
    df = (
        spend.join(g, on="date", how="left")
        .with_columns(
            (pl.col("attendance").fill_null(0) / 10000).alias("att_10k"),
            pl.col("date").dt.weekday().alias("dow"),
            pl.col("date").dt.month().alias("mon"),
            pl.col("date").dt.year().alias("yr"),
        )
    )
    pdf = df.to_pandas()
    model = smf.ols("spend_idx ~ att_10k + C(dow) + C(mon) + C(yr)", data=pdf).fit()
    ci = model.conf_int().loc["att_10k"]
    game = pdf[pdf["att_10k"] > 0]
    mean_att = float(game["att_10k"].mean() * 10000)
    b = float(model.params["att_10k"])
    summary = {
        "n_days": int(len(pdf)),
        "n_game_days": int(len(game)),
        "pp_per_10k": round(b * 100, 3),            # percentage points of county spend / 10k fans
        "se_pp_per_10k": round(float(model.bse["att_10k"]) * 100, 3),
        "p_value": round(float(model.pvalues["att_10k"]), 5),
        "ci95_pp_per_10k": [round(float(ci.iloc[0]) * 100, 3), round(float(ci.iloc[1]) * 100, 3)],
        "mean_attendance": round(mean_att),
        "lift_at_mean_pp": round(b * mean_att / 10000 * 100, 3),
    }
    return model, summary


def dollarize(
    pp_per_10k: float,
    attendance: float,
    sf_daily_spend_base_usd: float,
) -> dict:
    """Convert the calibrated %-lift into dollars for a game of `attendance` fans.

    `sf_daily_spend_base_usd` is the absolute SF-wide daily card-spend level the OI index
    is a deviation OF — it MUST be supplied (sourced from CDTFA/BEA), never assumed here.
    Returns event-day lift $ and $/attendee. These inherit the first-swing caveats above.
    """
    lift_frac = (pp_per_10k / 100) * (attendance / 10000)   # fractional lift for this crowd
    event_lift_usd = lift_frac * sf_daily_spend_base_usd
    return {
        "attendance": round(attendance),
        "lift_pct": round(lift_frac * 100, 2),
        "sf_daily_spend_base_usd": sf_daily_spend_base_usd,
        "event_day_lift_usd": round(event_lift_usd),
        "usd_per_attendee": round(event_lift_usd / attendance, 2) if attendance else None,
    }
