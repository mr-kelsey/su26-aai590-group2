"""Tier 1: pooled gradient-boosted counterfactual forecaster.

This is our measurement model. We train it on non-event hours only and give it no event features, so its residual on an event hour is causally readable. The model has never seen an event and cannot have partially fitted one. It is also the one artifact here with held-out ground truth. Control hours have observed outcomes, so the MAE and RMSE we report on them are real numbers rather than proxies.

We also use it as the benchmark we measure Tier 2 against. We have 452 nodes with dense data on each one, so a well-featured GBM is a strong baseline.

We train on clean_control_strict rather than the shipped clean_control, which leaves Chase Center, Moscone, citywide and street-fair days in the control pool. 828 days carry the shipped flag but only 581 are event-free. We ran both and the difference is -0.3% on test MAE.

We deliberately leave n_poi_reporting out of the features. It counts POIs with a non-zero hour, so person_hours = 0 forces it to 0 and it leaks the target. We use n_poi_live for panel health instead. It counts the distinct POIs reporting across a whole ISO week, and it tracks the 27% construction drift without encoding any single hour's outcome.
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
    # Three lookback depths. A single deep baseline reaches back a median of
    # 112 calendar days on game days and as far as 203, because ~47% of
    # same-dow candidates get skipped as event days. RMSE turns out to have an
    # interior optimum around k=3 to 4, and combining depths beats any single
    # one. cap120 is a RANGE ceiling. Tier 2 gets the same three, so the
    # ablation tests the graph and not the features.
    "base_k2", "base_k4", "base_cap120", "n_cap120",
    "hour", "dow", "month",
    "t_index",          # drift: days since window start
    "n_poi_live",       # drift: distinct POIs live in cell that ISO week
    "n_poi", "food_share", "dist_venue_m", "bearing_venue_deg",
    "temp_hr", "prcp_hr", "wind_hr",
    "us_federal_holiday",
]
CATEGORICAL = ["unit_code"]


def lgb_params(n_estimators: int = 600, seed: int = 0, **override) -> dict:
    """The LightGBM settings every GBM in this project trains under.

    They live in one place because the measurement model here and the full-window
    model in nowcast/effects.py are meant to be the same model on different training
    sets. If we kept two copies of the same dict they would drift apart, and we would
    not notice, because both copies would still fit and the effect estimate would
    stop being comparable to the held-out metrics.

    deterministic and force_row_wise are set so a rerun reproduces a published number
    exactly. Two fits under these settings return bit-identical residuals across all
    5,475 day by band cells. Without them LightGBM picks its histogram construction by
    a timing test, so a fit can depend on machine load. We have not characterised how
    large that variation gets, and we do not want a figure in a document to depend
    on it at all. These settings cost some fit speed.
    """
    p = dict(n_estimators=n_estimators, learning_rate=0.05, num_leaves=255,
             min_child_samples=100, subsample=0.8, subsample_freq=1,
             colsample_bytree=0.8, random_state=seed, verbose=-1, n_jobs=-1,
             deterministic=True, force_row_wise=True)
    p.update(override)
    return p


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

    common = lgb_params(n_estimators, seed)

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
