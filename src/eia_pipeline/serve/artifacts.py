"""Freeze the model, its features and its effect layer into a servable bundle.

The endpoint gets a date and a lat/lon and has to score all 452 cells across the
evening window. Doing that from parquet would drag polars and pyarrow into the
container for no reason, so everything lands as .npz and .json and the serving
dependency list stays exactly numpy, pandas and lightgbm.

LAYOUT AND WHY

    model.txt          the booster, LightGBM text format
    unit_ids.json      452 unit_ids; INDEX i holds the cell whose unit_code is i+1
    cells.npz          452 x cell statics
    dates.json         1,365 dates x game / event / provenance metadata
    timefeat.npz       1,365 x 8 hours x calendar and weather
    poi_live.npz       452 x 1,365 int16 (varies by cell and ISO week only)
    roll.npz           the rolling baselines at full grain
    effects.json       the effect layer from effects_v2
    golden.npz         256 rows of assembled X plus their predictions
    manifest.json      versions, row counts, sha256s, provenance

Rows are laid out so a request is one contiguous slice:

    row = ((date_idx * N_HOURS) + (hour - 16)) * N_CELLS + cell_idx
    cell_idx = unit_code - 1

so a date's 3,616 rows are roll[date_idx*3616 : (date_idx+1)*3616], no gather.

EVENING HOURS ONLY. game_dollars.py is explicit that the effects are estimated on
16-23 and that applying them to a whole day inflates the answer by roughly 3x.
Shipping only the hours the estimand supports makes that misuse impossible, and
cuts the array by 3x on the way.

ALL 1,365 DATES, not just the 328 game dates. It costs about 20 MB and it buys the
non-game answer: instead of a hardcoded zero the site can say "no home game,
expect about N visitor-hours on your block between 4 and 11pm", which is one more
hardcoded constant gone.

THE GOLDEN FIXTURE is the point of the whole module. 256 rows chosen to span the
extremes are re-assembled through the packaged featurespec at container start and
compared to the predictions recorded here. A mismatch raises inside model_fn,
which fails the health check, which means the endpoint goes to Failed instead of
InService. An endpoint that will not start beats one that answers wrongly.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import polars as pl

from ..settings import settings
from . import featurespec as fs
from .featurespec import EVENING, FEATURES
from .spine_2026 import PANEL_END, SERVE_END, SERVE_START, observed_end

N_HOURS = EVENING[1] - EVENING[0] + 1
CELL_STATICS = ["n_poi", "food_share", "dist_venue_m", "bearing_venue_deg", "lat", "lon"]
TIME_FEATURES = ["hour", "dow", "month", "t_index",
                 "temp_hr", "prcp_hr", "wind_hr", "us_federal_holiday"]
GOLDEN_N = 256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out_dir: Path | None = None, seed: int = 0) -> dict:
    """Assemble every artifact. Returns the manifest."""
    from .fitmodel import MODEL_TXT, cache_dir, load_cached

    out = Path(out_dir or (settings.data_dir / "serve_artifacts"))
    out.mkdir(parents=True, exist_ok=True)
    base = settings.data_dir / "bronze_sf"
    booster, cats = load_cached()

    mh = pl.read_parquet(base / "model_hour_serve.parquet")
    rb = pl.read_parquet(base / "rolling_baseline_serve.parquet")
    df = mh.join(rb, on=["unit_id", "date", "hour"], how="left")

    # unit_code must be the SAME dense rank the model was trained under. Derive it
    # from the sorted unit_id list, then assert it matches the booster's own
    # category set rather than hoping.
    units = sorted(df["unit_id"].unique().to_list())
    if len(units) != len(cats):
        raise RuntimeError(f"{len(units)} cells but booster knows {len(cats)} categories")
    code = {u: i + 1 for i, u in enumerate(units)}
    if list(cats) != list(range(1, len(units) + 1)):
        raise RuntimeError(f"booster categories are not 1..{len(units)}")

    df = df.with_columns(
        pl.col("unit_id").replace_strict(code, return_dtype=pl.Int32).alias("unit_code")
    ).sort(["date", "hour", "unit_code"])

    dates = df["date"].unique(maintain_order=True).sort().to_list()
    n_cells, n_dates = len(units), len(dates)
    expect = n_dates * N_HOURS * n_cells
    if df.height != expect:
        raise RuntimeError(f"{df.height:,} rows, expected {expect:,} "
                           f"({n_dates} dates x {N_HOURS} hours x {n_cells} cells)")

    # ---- the row-index contract, asserted rather than assumed
    di = {d: i for i, d in enumerate(dates)}
    idx = ((np.array([di[d] for d in df["date"]]) * N_HOURS
            + (df["hour"].to_numpy() - EVENING[0])) * n_cells
           + (df["unit_code"].to_numpy() - 1))
    if not np.array_equal(idx, np.arange(df.height)):
        raise RuntimeError("sort order does not match the documented row index")

    # ---- cell statics (one row per cell, in unit_code order)
    cd = df.group_by("unit_id").agg(
        *[pl.col(c).first() for c in CELL_STATICS]
    ).with_columns(
        pl.col("unit_id").replace_strict(code, return_dtype=pl.Int32).alias("unit_code")
    ).sort("unit_code")
    np.savez_compressed(out / "cells.npz",
                        **{c: cd[c].to_numpy().astype("float64") for c in CELL_STATICS})
    (out / "unit_ids.json").write_text(json.dumps(units) + "\n")

    # ---- time features (date x hour, cell-invariant)
    tf = df.group_by(["date", "hour"]).agg(
        *[pl.col(c).first() for c in TIME_FEATURES if c != "hour"]
    ).sort(["date", "hour"])
    np.savez_compressed(
        out / "timefeat.npz",
        hour=tf["hour"].to_numpy().astype("int8"),
        **{c: tf[c].to_numpy().astype(fs.DTYPES[c]) for c in TIME_FEATURES if c != "hour"},
    )

    # ---- n_poi_live: varies by cell and ISO week only, so its own (cell, date) grid
    pv = df.filter(pl.col("hour") == EVENING[0]).select(
        "date", "unit_code", "n_poi_live", "n_poi_live_held"
    ).sort(["date", "unit_code"])
    np.savez_compressed(
        out / "poi_live.npz",
        n_poi_live=pv["n_poi_live"].to_numpy().astype("int32").reshape(n_dates, n_cells),
        held=pv["n_poi_live_held"].to_numpy().astype("bool").reshape(n_dates, n_cells),
    )

    # ---- rolling baselines at full grain
    np.savez_compressed(
        out / "roll.npz",
        base_k2=df["base_k2"].to_numpy().astype("float32"),
        base_k4=df["base_k4"].to_numpy().astype("float32"),
        base_cap120=df["base_cap120"].to_numpy().astype("float32"),
        n_cap120=df["n_cap120"].to_numpy().astype("float32"),
    )

    # ---- per-date metadata
    obs_end = observed_end()
    att = _attendance_by_date()
    per_date = df.group_by("date").agg(
        pl.col("giants_home").first(), pl.col("n_games").first(),
        pl.col("first_pitch_hour").first(), pl.col("day_night").first(),
        pl.col("t_index").first(), pl.col("dow").first(), pl.col("month").first(),
        pl.col("observed").first(), pl.col("weather_source").first(),
        pl.col("attendance_pred").first(),
        pl.col("chase_day").first(), pl.col("moscone_day").first(),
        pl.col("citywide_day").first(), pl.col("street_fair_day").first(),
        pl.col("baseline_staleness_days").median().alias("stale"),
    ).sort("date")

    t_index_max_train = int(
        df.filter(pl.col("date") <= pl.lit(PANEL_END).str.to_date())["t_index"].max()
    )
    dates_json = []
    for r in per_date.iter_rows(named=True):
        d = str(r["date"])
        actual = att.get(d)
        a = actual if actual else r["attendance_pred"]
        dates_json.append({
            "date": d,
            "t_index": int(r["t_index"]),
            "dow": int(r["dow"]), "month": int(r["month"]),
            "giants_home": bool(r["giants_home"]),
            "n_games": int(r["n_games"] or 0),
            "first_pitch_hour": None if r["first_pitch_hour"] is None
                                else int(r["first_pitch_hour"]),
            "day_night": r["day_night"],
            "attendance": None if a is None else float(a),
            "attendance_source": ("actual" if actual else
                                  ("typical" if a is not None else None)),
            "observed": bool(r["observed"]),
            "projected": d > obs_end,
            "weather_source": r["weather_source"],
            "baseline_staleness_days": None if r["stale"] is None else int(r["stale"]),
            "t_index_beyond_training": int(r["t_index"]) > t_index_max_train,
            "chase_day": bool(r["chase_day"]),
            "moscone_day": bool(r["moscone_day"]),
            "citywide_day": bool(r["citywide_day"]),
            "street_fair_day": bool(r["street_fair_day"]),
        })
    (out / "dates.json").write_text(json.dumps(dates_json) + "\n")

    # ---- effect layer and booster, copied in
    eff_src = base / "effects.json"
    (out / "effects.json").write_text(eff_src.read_text())
    (out / "model.txt").write_text((cache_dir() / MODEL_TXT).read_text())

    # ---- golden fixture
    golden = _write_golden(out, df, booster, cats, seed=seed)

    manifest = {
        "schema": "oracle-ripple-artifacts/1",
        "serve_window": [SERVE_START, SERVE_END],
        "training_window": ["2023-01-02", PANEL_END],
        "observed_panel_through": obs_end,
        "evening_hours": list(EVENING),
        "n_cells": n_cells,
        "n_dates": n_dates,
        "n_rows": int(df.height),
        "row_index": "((date_idx * n_hours) + (hour - 16)) * n_cells + (unit_code - 1)",
        "features": FEATURES,
        "t_index_max_training": t_index_max_train,
        "golden_n": golden["n"],
        "featurespec_sha256": _sha256(Path(fs.__file__)),
        "files": {p.name: _sha256(p) for p in sorted(out.glob("*")) if p.is_file()},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    total = sum(p.stat().st_size for p in out.glob("*") if p.is_file())
    print(f"  artifacts: {n_cells} cells x {n_dates} dates x {N_HOURS} hours = "
          f"{df.height:,} rows, {total / 1e6:.1f} MB -> {out}", flush=True)
    return manifest


def _attendance_by_date() -> dict:
    from ..io import duckdb_s3
    from .spine_2026 import MLB, _src

    rows = duckdb_s3().execute(
        f"""SELECT date::DATE AS date, max(TRY_CAST(attendance AS INTEGER)) AS att
            FROM read_csv('{_src(MLB)}', all_varchar=true)
            WHERE TRY_CAST(attendance AS INTEGER) > 0 GROUP BY 1"""
    ).pl()
    return {str(d): float(a) for d, a in zip(rows["date"], rows["att"])}


def _write_golden(out: Path, df: pl.DataFrame, booster, cats,
                  n: int = GOLDEN_N, seed: int = 0) -> dict:
    """Rows chosen to span the extremes, not sampled uniformly.

    A uniform sample would be 99% ordinary interior rows and would not exercise
    the cases that actually break: the innermost and outermost cells, warm-up rows
    where base_k2 is NaN, n_cap120 = 0, holidays, projected dates, and every month.
    """
    rng = np.random.default_rng(seed)
    picks: list[int] = []

    def take(mask: pl.Series, k: int) -> None:
        idx = np.flatnonzero(mask.to_numpy())
        if idx.size:
            picks.extend(rng.choice(idx, min(k, idx.size), replace=False).tolist())

    dmin, dmax = df["dist_venue_m"].min(), df["dist_venue_m"].max()
    take(df["dist_venue_m"] == dmin, 16)
    take(df["dist_venue_m"] == dmax, 16)
    take(df["base_k2"].is_null(), 24)
    take(df["n_cap120"] == 0, 8)
    take(df["us_federal_holiday"].cast(pl.Int8) == 1, 16)
    take(~df["observed"], 24)
    take(df["giants_home"], 24)
    for m in range(1, 13):
        take(df["month"] == m, 6)
    take(pl.Series([True] * df.height), max(0, n - len(picks)))

    sel = np.array(sorted(set(picks))[:n])
    g = df[sel.tolist()]
    X = fs.build_X({f: g[f].to_numpy() for f in FEATURES}, cats)
    yhat = fs.predict_cf(booster, X)
    np.savez_compressed(
        out / "golden.npz",
        row=sel.astype("int64"),
        yhat=yhat.astype("float64"),
        **{f: X[f].astype("float64").to_numpy() if f not in fs.CATEGORICAL
           else np.asarray(X[f].cat.codes, dtype="int32") for f in FEATURES},
    )
    return {"n": int(len(sel))}


def verify(out_dir: Path | None = None) -> None:
    """Re-read everything and rebuild the golden rows through the packaged path."""
    import lightgbm as lgb

    out = Path(out_dir or (settings.data_dir / "serve_artifacts"))
    man = json.loads((out / "manifest.json").read_text())
    booster = lgb.Booster(model_file=str(out / "model.txt"))
    cats = booster.pandas_categorical[0]
    g = np.load(out / "golden.npz")

    cols = {}
    for f in FEATURES:
        cols[f] = (np.asarray(cats)[g[f]] if f in fs.CATEGORICAL else g[f])
    X = fs.build_X(cols, cats)
    yhat = fs.predict_cf(booster, X)
    np.testing.assert_allclose(yhat, g["yhat"], atol=1e-9)

    got = _sha256(Path(fs.__file__))
    if got != man["featurespec_sha256"]:
        raise RuntimeError("featurespec.py changed since the artifacts were built")
    print(f"  OK  golden {len(yhat)} rows reproduce to 1e-9, featurespec sha matches",
          flush=True)


if __name__ == "__main__":  # pragma: no cover
    build()
    verify()
