"""Forecasting a game night, and scoring how well we can do it.

The two model tiers are counterfactual. They train on control hours with no event
features, so when we ask them about 8pm on a game night they answer with the no-game
number. That is what makes the residual causally readable. It also means neither tier
is a forecaster on its own.

We build the forecast by composing the two pieces we have:

    predicted game-night presence = counterfactual(date, no game) x (1 + band effect)

Every input is available before a game is played. The counterfactual needs only
calendar, weather and the trailing baseline, and we estimate the band effect from
games already played. So this is a real forecast rather than a description, and this
module puts a held-out number on it.

We made three choices in setting up the evaluation.

We score the shipped Tier 1 model rather than a re-specified one. The counterfactual
comes straight from tier1_gbm.fit(), which trains on train-split control hours only,
so nothing in the val or test period informs it.

We estimate the band effects on train-split games only. If we estimated them over the
full window and then scored on test games, the test games would inform the effects we
score them against. We measured that leak here and it comes to about 0.7% on test MAE.

The band effects we estimate here do not match the ones in nowcast/effects.py, even on
the same days. effects.py fits its counterfactual on strict-control hours from every
split, because it is measuring an effect and wants the best calibration it can get
across the window. We have to fit on train hours only, because we are forecasting and
cannot see the held-out period. On 2023 and 2024 that gives us +41.4% at 0 to 500m
where effects.py gives +37.3%. The scores below are not affected, since they use our
numbers all the way through, but the effects printed by score() should not be quoted
as the measured effect.

We report by year as well as by split. The panel thins sharply across the window and
the held-out period sits entirely in the thinnest year, so a single held-out number
would rest on the least trustworthy stretch of data. The year breakdown shows whether
the composition works in every panel regime or only one. It is keyed on split too,
because the split is temporal: 2023 and 2024 are training years and only 2025 is held
out, so the years are not three independent tests. See docs/04_data_exploration.md
section 7 for what the thinning does to the effect estimate itself.

We do not score against a naive mean. The reference is the counterfactual on its own,
which amounts to ignoring the game entirely. Anything the composition adds over that
is the predictive content of the measured effect.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from .effects import BANDS, EVENING


def band_effects(model, df: pl.DataFrame, split: str = "train",
                 hours: tuple = EVENING) -> dict:
    """Log-space DiD effect per band, estimated from one split's games only.

    Returns a dict of band name to log-space effect, which is what compose() wants.
    Keep `split` at train for any evaluation that scores val or test.
    """
    from .effects import day_band_residuals, _did

    sub = df.filter(pl.col("split") == split)
    db = day_band_residuals(model, sub, hours=hours)
    gd = db.filter(pl.col("giants_home"))["date"].unique().to_list()
    cd = db.filter(pl.col("is_control") & ~pl.col("giants_home"))["date"].unique().to_list()
    if not gd or not cd:
        raise ValueError(f"split '{split}' has {len(gd)} game and {len(cd)} control days")
    return _did(db, gd, cd)


def compose(cf_log: np.ndarray, dist_m: np.ndarray, is_game: np.ndarray,
            eff: dict) -> np.ndarray:
    """Apply the band multiplier to a counterfactual prediction.

    The counterfactual is in log1p space and the effect is a ratio, so we go back to
    levels to multiply and then return. Applying the effect additively in log space
    would be wrong, and the error is hard to spot. At large counts the two approaches
    agree to within a fraction of a percent. They only diverge where the counts are
    small, which is most of this panel.

    Bands are assigned first-match-wins on the upper bound, the same cascade the
    effects were estimated under, so a row is multiplied by the effect that was
    measured on rows like it. Non-game rows pass through untouched.
    """
    mult = np.ones(len(cf_log), dtype=float)
    taken = np.zeros(len(cf_log), dtype=bool)
    for name, _lo, hi in BANDS:
        sel = is_game & ~taken & (dist_m <= hi)
        mult[sel] = np.exp(eff.get(name, 0.0))
        taken |= sel
    return np.log1p(np.expm1(np.clip(cf_log, 0, None)) * mult)


def score(control_col: str = "clean_control_strict", n_estimators: int = 600,
          seed: int = 0, hours: tuple = EVENING) -> dict:
    """Fit on train controls, take effects from train games, score held-out game evenings.

    Returns the per-band and per-year breakdown plus the fitted pieces, so a caller
    can forecast a future date without refitting.
    """
    from .models import tier1_gbm

    res = tier1_gbm.fit(control_col, n_estimators=n_estimators, seed=seed)
    meas = res["_model"]
    df = tier1_gbm.load(control_col)

    eff = band_effects(meas, df, split="train", hours=hours)
    print("  forecast band effects, from train games and the train-fitted counterfactual.")
    print("  These are not the measured effect, see the module docstring.")
    print("    " + "  ".join(f"{b}={(np.exp(v) - 1) * 100:+.1f}%" for b, v in eff.items()))

    X, y = tier1_gbm._xy(df)
    cf = meas.predict(X)
    is_game = df["giants_home"].to_numpy().astype(bool)
    dist = df["dist_venue_m"].to_numpy()
    comp = compose(cf, dist, is_game, eff)

    hr = df["hour"].to_numpy()
    ev = is_game & (hr >= hours[0]) & (hr <= hours[1])

    frame = pl.DataFrame({
        "split": df["split"], "year": df["date"].dt.year(),
        "dist": dist, "y": y, "cf": cf, "comp": comp, "ev": ev,
    }).filter(pl.col("ev"))

    def _band(d):
        e = pl.when(pl.col("dist") <= BANDS[0][2]).then(pl.lit(BANDS[0][0]))
        for nm, lo, hi in BANDS[1:]:
            e = e.when(pl.col("dist") <= hi).then(pl.lit(nm))
        return d.with_columns(e.otherwise(pl.lit(BANDS[-1][0])).alias("band"))

    frame = _band(frame).with_columns(
        (pl.col("y") - pl.col("cf")).abs().alias("e_cf"),
        (pl.col("y") - pl.col("comp")).abs().alias("e_comp"),
    )

    def _agg(d, keys):
        return d.group_by(keys).agg(
            pl.len().alias("n"),
            pl.col("e_cf").mean().alias("mae_ignore_game"),
            pl.col("e_comp").mean().alias("mae_composition"),
        ).with_columns(
            ((pl.col("mae_ignore_game") - pl.col("mae_composition"))
             / pl.col("mae_ignore_game") * 100).alias("improvement_pct")
        ).sort(keys)

    out = {
        "effects_train": {b: float(v) for b, v in eff.items()},
        "by_split": _agg(frame, ["split"]),
        "by_split_band": _agg(frame, ["split", "band"]),
        # keyed on split as well as year on purpose. The split is temporal, so 2023
        # and 2024 are train years and only 2025 is held out. A table keyed on year
        # alone reads as three independent out-of-sample years, which it is not.
        "by_year": _agg(frame, ["year", "split"]),
        "by_year_band": _agg(frame.filter(pl.col("band") == BANDS[0][0]),
                             ["year", "split"]),
        "_model": meas,
        "_effects": eff,
    }
    return out


def forecast(model, eff: dict, rows: pl.DataFrame) -> np.ndarray:
    """Forecast game-night presence for rows that carry the Tier 1 feature set.

    `rows` needs the tier1_gbm features plus dist_venue_m and giants_home. Returns
    log1p presence, so expm1 it for a level.
    """
    from .models import tier1_gbm

    X, _ = tier1_gbm._xy(rows)
    cf = model.predict(X)
    return compose(cf, rows["dist_venue_m"].to_numpy(),
                   rows["giants_home"].to_numpy().astype(bool), eff)
