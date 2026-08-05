"""Drift-corrected game-day effect estimation over the full window.

We made three choices in setting this up.

First, we fit the counterfactual model on all strict-control hours across every split rather than just train. It never sees a treated hour in any split, so residuals on game hours stay causally readable, but it is far better calibrated across the whole window, which matters because of the 27% construction drift. The held-out forecasting metrics in tier1_gbm stand separately as our evidence on model quality. We fit this model to measure effects, and we do not use it to show generalisation.

Second, effects are a difference-in-differences on residuals: the game-hour residual minus the contemporaneous control-hour residual. The model over-predicts by a drifting amount (bias +0.164 on val, +0.254 on test), and differencing against controls from the same period cancels it. A raw residual would absorb the drift instead.

Third, we cluster inference at the day, because cell-hours within a day are heavily correlated. Naive cell-hour t-stats overstate significance by roughly 3x: the 0-500m band reads t=7.2 unclustered against z=+2.2 when compared to placebo. So we collapse residuals to day x band means first and bootstrap over days.
"""
from __future__ import annotations

import numpy as np
import polars as pl

BANDS = [
    ("0-500m", 0, 500),
    ("500m-1km", 500, 1000),
    ("1-2km", 1000, 2000),
    ("2-4km", 2000, 4000),
    (">4km", 4000, 10**9),
]
EVENING = (16, 23)


def _band_expr(col: str = "dist_venue_m") -> pl.Expr:
    e = pl.when(pl.col(col) <= BANDS[0][2]).then(pl.lit(BANDS[0][0]))
    for name, lo, hi in BANDS[1:]:
        e = e.when(pl.col(col) <= hi).then(pl.lit(name))
    return e.otherwise(pl.lit(BANDS[-1][0])).alias("band")


def fit_full_control_model(n_estimators: int = 600, seed: int = 0):
    """Train on every strict-control hour, all splits. Returns (model, frame)."""
    import lightgbm as lgb
    from .models.tier1_gbm import load, _xy, lgb_params

    df = load("clean_control_strict")
    ctl = df.filter(pl.col("is_control"))
    X, y = _xy(ctl)
    m = lgb.LGBMRegressor(objective="l2", **lgb_params(n_estimators, seed))
    m.fit(X, y)
    print(f"  fit on {ctl.height:,} strict-control hours (all splits)")
    return m, df


def day_band_residuals(model, df: pl.DataFrame,
                       hours: tuple = EVENING) -> pl.DataFrame:
    """Collapse residuals to day x band means, the clustering unit for inference."""
    from .models.tier1_gbm import _xy

    X, _ = _xy(df)
    df = df.with_columns(pl.Series("pred", model.predict(X)))
    df = df.with_columns((pl.col("y") - pl.col("pred")).alias("resid"))
    ev = df.filter(pl.col("hour").is_between(*hours)).with_columns(_band_expr())
    return ev.group_by(["date", "band"]).agg(
        pl.col("resid").mean().alias("resid"),
        pl.col("giants_home").first().alias("giants_home"),
        pl.col("is_control").first().alias("is_control"),
    )


def day_band_residuals_from_grid(pred: np.ndarray, data: dict,
                                 hours: tuple = EVENING) -> pl.DataFrame:
    """Same readout as `day_band_residuals`, but for a Tier 2 [T, N] prediction grid.

    Tier 1 predicts row-wise and Tier 2 predicts on a dense time x node grid, so
    without this they could not be compared without re-deriving the estimator and
    risking a subtle difference doing the comparing for us. Both paths land in the
    identical day x band frame and go through the identical bootstrap.

    `data` is the dict from tier2_stgnn.build_tensors(); `pred` must align with
    its y/mask grid exactly.
    """
    from .models.tier1_gbm import load

    T, N = data["T"], data["N"]
    if pred.shape != (T, N):
        raise ValueError(f"pred {pred.shape} != grid ({T}, {N})")

    units = data["units"]
    ts = data["ts"]
    grid = pl.DataFrame({
        "date": np.repeat(ts["date"].to_numpy(), N),
        "hour": np.repeat(ts["hour"].to_numpy(), N),
        "unit_id": np.tile(np.array(units, dtype=object), T),
        "pred": pred.reshape(-1),
        "y": data["y"].reshape(-1),
    })
    meta = load("clean_control_strict").select(
        ["unit_id", "date", "hour", "dist_venue_m", "giants_home", "is_control"])
    df = grid.join(meta, on=["unit_id", "date", "hour"], how="inner")
    df = df.with_columns((pl.col("y") - pl.col("pred")).alias("resid"))
    # predict_grid has no convolution history for the leading CONTEXT hours and
    # leaves them NaN. NaN is a value rather than a null, so it survives the join
    # and makes every band mean it enters NaN: one NaN residual takes out the
    # whole day x band cell, and if that date is in the control pool the DiD goes
    # NaN for every band at once with no error raised. Only 2023-01-02 is
    # affected, and it is outside the control pool today only because the
    # Warriors were at home that night.
    df = df.filter(pl.col("resid").is_not_nan() & pl.col("resid").is_not_null())
    ev = df.filter(pl.col("hour").is_between(*hours)).with_columns(_band_expr())
    return ev.group_by(["date", "band"]).agg(
        pl.col("resid").mean().alias("resid"),
        pl.col("giants_home").first().alias("giants_home"),
        pl.col("is_control").first().alias("is_control"),
    )


def _day_mean(dayband: pl.DataFrame, days, name: str) -> pl.DataFrame:
    """Mean residual per band over `days`, counting a day once per appearance.

    This joins rather than filters, and the difference matters. A bootstrap draw
    contains duplicates, but is_in is a set-membership test, so filtering keeps a
    day drawn three times exactly once. That turns every replicate into a sample of
    about 63% of the days drawn without replacement, and a mean over a sample that
    size carries a finite-population correction that shrinks its variance. We
    measured the result at roughly 24% too narrow, so intervals came out too tight
    and p-values too small. Joining against the drawn list keeps the duplicates and
    gives a real bootstrap. Point estimates are unaffected either way, because the
    observed day lists have no duplicates in them.
    """
    sel = pl.DataFrame({"date": list(days)}, schema={"date": dayband.schema["date"]})
    return sel.join(dayband, on="date", how="inner").group_by("band").agg(
        pl.col("resid").mean().alias(name))


def _did(dayband: pl.DataFrame, game_days, ctrl_days) -> dict:
    g = _day_mean(dayband, game_days, "g")
    c = _day_mean(dayband, ctrl_days, "c")
    j = g.join(c, on="band")
    return {r["band"]: r["g"] - r["c"] for r in j.iter_rows(named=True)}


def estimate(dayband: pl.DataFrame, n_boot: int = 2000, seed: int = 0) -> pl.DataFrame:
    """Day-clustered bootstrap CI for the DiD effect in each distance band."""
    rng = np.random.default_rng(seed)
    gd = dayband.filter(pl.col("giants_home"))["date"].unique().to_numpy()
    cd = dayband.filter(pl.col("is_control") & ~pl.col("giants_home"))["date"].unique().to_numpy()
    point = _did(dayband, gd.tolist(), cd.tolist())

    draws = {b: [] for b in point}
    for _ in range(n_boot):
        d = _did(dayband,
                 rng.choice(gd, size=len(gd), replace=True).tolist(),
                 rng.choice(cd, size=len(cd), replace=True).tolist())
        for b, v in d.items():
            draws[b].append(v)

    rows = []
    for name, _, _ in BANDS:
        a = np.array(draws[name])
        rows.append({
            "band": name, "n_game_days": int(len(gd)), "n_ctrl_days": int(len(cd)),
            "did": point[name],
            "effect_pct": (np.exp(point[name]) - 1) * 100,
            "lo95": float(np.percentile(a, 2.5)),
            "hi95": float(np.percentile(a, 97.5)),
            "lo95_pct": (np.exp(np.percentile(a, 2.5)) - 1) * 100,
            "hi95_pct": (np.exp(np.percentile(a, 97.5)) - 1) * 100,
            "p_two_sided": float(2 * min((a <= 0).mean(), (a >= 0).mean())),
        })
    return pl.DataFrame(rows)


def placebo(dayband: pl.DataFrame, n_draws: int = 200, seed: int = 0) -> pl.DataFrame:
    """Falsification: draw fake 'game' days from control days and re-estimate."""
    rng = np.random.default_rng(seed)
    gd = dayband.filter(pl.col("giants_home"))["date"].unique().to_numpy()
    cd = dayband.filter(pl.col("is_control") & ~pl.col("giants_home"))["date"].unique().to_numpy()
    draws = {b: [] for b, _, _ in BANDS}
    for _ in range(n_draws):
        fake = rng.choice(cd, size=len(gd), replace=False)
        rest = np.setdiff1d(cd, fake)
        d = _did(dayband, fake.tolist(), rest.tolist())
        for b, v in d.items():
            draws[b].append(v)
    return pl.DataFrame([
        {"band": b, "placebo_mean": float(np.mean(draws[b])),
         "placebo_sd": float(np.std(draws[b]))}
        for b, _, _ in BANDS
    ])
