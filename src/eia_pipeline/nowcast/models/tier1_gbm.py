"""Tier 1: pooled gradient-boosted counterfactual forecaster.

This is our measurement model. We train it on non-event hours only and give it no event features, so its residual on an event hour is causally readable: the model has never seen an event and cannot have partially fitted one. It is also the one artifact here with genuine held-out ground truth, since control hours have observed outcomes and the MAE and RMSE we report on them are real numbers rather than proxies. Very little else in this project can be validated that directly.

It doubles as the benchmark Tier 2 has to beat. With 452 nodes and dense data on each one, a well-featured GBM is hard to improve on, and we would rather report that honestly than bury it.

We train on clean_control_strict rather than the shipped clean_control, because the shipped flag leaves Chase Center, Moscone, citywide and street-fair days in the control pool: 828 days flagged against 581 that are genuinely clean. We evaluate both so the sensitivity is something we measured rather than something we assumed.

We deliberately leave n_poi_reporting out of the features. It counts POIs with a non-zero hour, so person_hours = 0 forces it to 0 and it leaks the target. Panel health enters instead as n_poi_live, the distinct POIs reporting across a whole ISO week, which tracks the 27% construction drift without encoding any single hour's outcome.
"""
from __future__ import annotations

import numpy as np
import polars as pl

MODEL_HOUR = "data/bronze_sf/model_hour.parquet"
WEEK_COV = "data/bronze_sf/cell_week_coverage.parquet"
ROLL_BASE = "data/bronze_sf/rolling_baseline.parquet"

TARGET = "person_hours"

FEATURES = [
    "unit_code",        # categorical, 452 cells
    # Option B, at three lookback depths rather than one. k=8 alone spanned a
    # median 112 calendar days on game days (max 203) because ~47% of same-dow
    # candidates are event days and get skipped. RMSE has an interior optimum
    # near k=3-4, and combining depths beats any single one. cap120 is a RANGE
    # ceiling, free on accuracy, taken to stay inside v0's +/-120 convention.
    # Given to Tier 2 as well, so the ablation tests the graph not the features.
    "base_k2", "base_k4", "base_cap120", "n_cap120",
    "hour", "dow", "month",
    "t_index",          # drift: days since window start
    "n_poi_live",       # drift: distinct POIs live in cell that ISO week
    "n_poi", "food_share", "dist_venue_m", "bearing_venue_deg",
    "temp_hr", "prcp_hr", "wind_hr",
    "us_federal_holiday",
]
CATEGORICAL = ["unit_code"]


def load(control_col: str = "clean_control_strict") -> pl.DataFrame:
    """Model frame with week-coverage joined and unit_id integer-coded."""
    df = pl.read_parquet(MODEL_HOUR)
    cov = pl.read_parquet(WEEK_COV)
    df = df.with_columns(
        pl.col("date").dt.truncate("1w").cast(pl.Date).alias("week_start")
    ).join(cov, on=["unit_id", "week_start"], how="left")
    df = df.join(pl.read_parquet(ROLL_BASE), on=["unit_id", "date", "hour"], how="left")
    df = df.with_columns(
        pl.col("n_poi_live").fill_null(0),
        pl.col("unit_id").rank("dense").cast(pl.Int32).alias("unit_code"),
        pl.col("us_federal_holiday").cast(pl.Int8),
        (pl.col(TARGET).log1p()).alias("y"),
        pl.col(control_col).alias("is_control"),
    )
    return df


def _xy(df: pl.DataFrame):
    X = df.select(FEATURES).to_pandas()
    for c in CATEGORICAL:
        X[c] = X[c].astype("category")
    return X, df["y"].to_numpy()


def baselines(train: pl.DataFrame, test: pl.DataFrame) -> dict:
    """Hour-of-week cell mean, and a global hour-of-week mean. Both on log target."""
    how = train.group_by(["unit_code", "dow", "hour"]).agg(pl.col("y").mean().alias("b_cell"))
    glob = train.group_by(["dow", "hour"]).agg(pl.col("y").mean().alias("b_glob"))
    t = test.join(how, on=["unit_code", "dow", "hour"], how="left").join(
        glob, on=["dow", "hour"], how="left"
    )
    t = t.with_columns(pl.col("b_cell").fill_null(pl.col("b_glob")))
    y = t["y"].to_numpy()
    out = {}
    for name, col in (("cell_hour_of_week", "b_cell"), ("global_hour_of_week", "b_glob")):
        p = t[col].to_numpy()
        out[name] = {"mae": float(np.mean(np.abs(y - p))),
                     "rmse": float(np.sqrt(np.mean((y - p) ** 2)))}
    return out


def fit(control_col: str = "clean_control_strict", n_estimators: int = 600,
        quantiles: tuple = (0.05, 0.5, 0.95), seed: int = 0) -> dict:
    """Train on control hours in the train split; evaluate on held-out control hours."""
    import lightgbm as lgb

    df = load(control_col)
    ctl = df.filter(pl.col("is_control"))
    tr = ctl.filter(pl.col("split") == "train")
    va = ctl.filter(pl.col("split") == "val")
    te = ctl.filter(pl.col("split") == "test")
    print(f"  control='{control_col}'  train={tr.height:,}  val={va.height:,}  test={te.height:,}")

    Xtr, ytr = _xy(tr)
    Xva, yva = _xy(va)
    Xte, yte = _xy(te)

    common = dict(n_estimators=n_estimators, learning_rate=0.05, num_leaves=255,
                  min_child_samples=100, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, random_state=seed, verbose=-1, n_jobs=-1)

    med = lgb.LGBMRegressor(objective="l2", **common)
    med.fit(Xtr, ytr, eval_set=[(Xva, yva)],
            callbacks=[lgb.early_stopping(50, verbose=False)])
    res = {"best_iter": int(med.best_iteration_ or n_estimators)}

    for split, X, y in (("val", Xva, yva), ("test", Xte, yte)):
        p = med.predict(X)
        res[split] = {"mae": float(np.mean(np.abs(y - p))),
                      "rmse": float(np.sqrt(np.mean((y - p) ** 2))),
                      "bias": float(np.mean(p - y)),
                      "r2": float(1 - np.sum((y - p) ** 2) / np.sum((y - y.mean()) ** 2))}

    # quantile heads -> interval calibration (also the drift diagnostic: a level
    # shift the model failed to absorb shows up as coverage collapsing on test)
    qm = {}
    for q in (quantiles[0], quantiles[-1]):
        m = lgb.LGBMRegressor(objective="quantile", alpha=q, **common)
        m.fit(Xtr, ytr)
        qm[q] = m
    for split, X, y in (("val", Xva, yva), ("test", Xte, yte)):
        lo, hi = qm[quantiles[0]].predict(X), qm[quantiles[-1]].predict(X)
        res[split]["coverage_90"] = float(np.mean((y >= lo) & (y <= hi)))

    res["baselines"] = {"val": baselines(tr, va), "test": baselines(tr, te)}
    res["importance"] = dict(sorted(
        zip(FEATURES, med.feature_importances_.tolist()), key=lambda kv: -kv[1]))
    res["_model"] = med
    return res
