"""The deployment model: forecasts presence including the event.

This is the other half of the pair, and the two cannot be one model.

  Measurement model   trained on control hours, no event features. Its residual on a game hour is causally readable precisely because it has never seen a game. It cannot forecast one: asked about 8pm on a game night it answers with the no-game number.

  Deployment model    trained on all hours with event features. It forecasts what will actually happen. Its residuals are not causal and must never be fed to the effect estimator, because it has already fitted the event and the effect would partly vanish into the fit.

The property that makes each one work destroys the other, so keeping them separate is not fastidiousness. Mixing them silently corrupts either the forecast or the causal claim.

We validate this on held-out game hours rather than control hours. A forecaster that scores well on quiet Tuesdays has demonstrated nothing about the night it is meant to predict. That also means its headline number is not comparable to the measurement model's, since it is a different evaluation set and a harder problem.

The baseline it has to beat is not the naive mean. It is the composition already implicit in the dollars bridge:

    predicted event presence = counterfactual x (1 + band effect)

That composition is free, so a trained deployment model only earns its place by beating it. We expected it would, since it can condition on attendance, first-pitch time and hour-relative-to-event per cell where the composition applies one band-average number to every game alike. It does not, at least in the near field. See the near_weight note in fit() and the PIPELINE.md gap entry for how that was established.
"""
from __future__ import annotations

import numpy as np
import polars as pl

from ...io import duckdb_s3
from ...ingest.advan_bronze import medallion_uri
from .tier1_gbm import load as load_base, FEATURES as BASE_FEATURES, CATEGORICAL

EVENT_FEATURES = [
    "giants_home", "n_games", "attendance", "day_night_code",
    "first_pitch_hour", "relative_hour",
    "chase_day", "chase_event_hour", "moscone_day",
    "citywide_day", "street_fair_day",
]
FEATURES = BASE_FEATURES + EVENT_FEATURES

# Band effects used by the free composition benchmark (see module docstring).
BAND_EFFECT = [(500, 44.6), (1000, 14.8), (2000, 4.5), (4000, 1.2), (10**9, 0.0)]


def load(con=None) -> pl.DataFrame:
    """Base frame plus real attendance and the event covariates, on ALL hours."""
    con = con or duckdb_s3()
    cal = con.execute(f"""
        SELECT date, attendance
        FROM read_parquet('{medallion_uri("silver", "calendar_day.parquet")}')
    """).pl()
    df = load_base("clean_control_strict").join(cal, on="date", how="left")
    return df.with_columns(
        # attendance is null on non-game days, and that is informative, not missing,
        # and LightGBM splits on it directly. Do not impute a zero.
        pl.col("attendance").cast(pl.Float64),
        pl.when(pl.col("day_night") == "night").then(1)
          .when(pl.col("day_night") == "day").then(0)
          .otherwise(None).cast(pl.Int8).alias("day_night_code"),
        *[pl.col(c).cast(pl.Int8) for c in
          ("giants_home", "chase_day", "chase_event_hour", "moscone_day",
           "citywide_day", "street_fair_day")],
    )


def _xy(df: pl.DataFrame):
    X = df.select(FEATURES).to_pandas()
    for c in CATEGORICAL:
        X[c] = X[c].astype("category")
    return X, df["y"].to_numpy()


def composition_benchmark(df: pl.DataFrame, meas_model) -> np.ndarray:
    """counterfactual x (1 + band effect), the free alternative, in log1p space."""
    from .tier1_gbm import _xy as base_xy

    Xb, _ = base_xy(df)
    cf = meas_model.predict(Xb)                      # log1p no-event level
    d = df["dist_venue_m"].to_numpy()
    e = np.zeros_like(d, dtype=float)
    prev = 0.0
    for edge, pct in BAND_EFFECT:
        e = np.where((d > prev) & (d <= edge), pct, e)
        prev = edge
    game = df["giants_home"].to_numpy().astype(bool)
    mult = np.where(game, 1 + e / 100, 1.0)
    # scale in level space, return to log1p
    return np.log1p(np.expm1(np.clip(cf, 0, None)) * mult)


def fit(n_estimators: int = 900, seed: int = 0, con=None,
        near_weight: float = 1.0) -> dict:
    """Train on all hours; score on held-out GAME hours in val/test."""
    import lightgbm as lgb
    from .tier1_gbm import fit as fit_measurement

    df = load(con)
    tr = df.filter(pl.col("split") == "train")
    va = df.filter(pl.col("split") == "val")
    te = df.filter(pl.col("split") == "test")

    Xtr, ytr = _xy(tr); Xva, yva = _xy(va); Xte, yte = _xy(te)

    # The near field is ~1% of rows, so a globally-optimised loss has almost no
    # incentive to fit it -- and unweighted, this model loses to the free
    # composition benchmark at 0-500m by 14%. near_weight upweights cells within
    # 1 km so the stratum that actually matters can influence the fit.
    w = None
    if near_weight != 1.0:
        w = np.where(tr["dist_venue_m"].to_numpy() <= 1000, near_weight, 1.0)
    m = lgb.LGBMRegressor(objective="l2", n_estimators=n_estimators, learning_rate=0.05,
                          num_leaves=255, min_child_samples=100, subsample=0.8,
                          subsample_freq=1, colsample_bytree=0.8, random_state=seed,
                          verbose=-1, n_jobs=-1)
    m.fit(Xtr, ytr, sample_weight=w, eval_set=[(Xva, yva)],
          callbacks=[lgb.early_stopping(50, verbose=False)])

    meas = fit_measurement("clean_control_strict", n_estimators=800)["_model"]

    res = {"best_iter": int(m.best_iteration_ or n_estimators), "_model": m,
           "_meas": meas}   # returned so callers need not refit it
    for name, sub, X, y in (("val", va, Xva, yva), ("test", te, Xte, yte)):
        game = sub["giants_home"].to_numpy().astype(bool)
        ev = sub["hour"].to_numpy()
        sel = game & (ev >= 16) & (ev <= 23)          # game evenings: the target case
        pred = m.predict(X)
        from .tier1_gbm import _xy as base_xy
        Xb, _ = base_xy(sub)
        cf = meas.predict(Xb)
        comp = composition_benchmark(sub, meas)
        mae = lambda p: float(np.mean(np.abs(y[sel] - p[sel])))
        res[name] = {"n_game_hours": int(sel.sum()),
                     "deploy_mae": mae(pred),
                     "counterfactual_mae": mae(cf),
                     "composition_mae": mae(comp),
                     "deploy_bias": float(np.mean(pred[sel] - y[sel])),
                     "deploy_r2": float(1 - np.sum((y[sel] - pred[sel]) ** 2)
                                        / np.sum((y[sel] - y[sel].mean()) ** 2))}
    res["importance"] = dict(sorted(zip(FEATURES, m.feature_importances_.tolist()),
                                    key=lambda kv: -kv[1]))
    return res
