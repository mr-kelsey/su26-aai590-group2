"""Serve layer for the Tier 2 STGNN (flow arm): checkpoint, cf grid, effects.

The second model behind the website's toggle. Same wire contract as the GBM
endpoint (schema oracle-ripple/1, 452 cells, visitor-hours over 16-23); only the
counterfactual source differs. Three design decisions worth restating:

1. THE FORWARD PASS RUNS HERE, NOT IN THE CONTAINER. The STGNN is deterministic
   given the serve tensors, exactly as the GBM is deterministic given the baked
   .npz arrays, so precomputing the counterfactual grid loses no liveness. What
   it buys: no torch in the container (the tarball stays ~20 MB against torch's
   multi-GB base image), no 48-hour-window tensors shipped, and the same
   fail-at-startup golden-fixture story via an index fixture.

2. THE MODEL IS THE TRAIN-SPLIT CHECKPOINT, honestly disclosed. The served GBM
   is refit on all splits; the STGNN has no such refit (its published numbers
   are train-split checkpoints), so we serve the same thing we measured. 2025
   is therefore extrapolation for this arm in a way it is not for the GBM.
   The manifest carries this.

3. t_index IS CLAMPED at its training-panel maximum before normalisation.
   `STGNN.inp` is a bare nn.Linear, so t_index enters with no saturation, unlike
   the GBM whose trees flatline past their last split. Train-split z-scoring
   puts 2026-09-27 at z=+4.7 against a training range of about +/-1.7, which
   would multiply whatever weight absorbed the panel's 27% coverage drift by
   ~2.7x, silently. Clamping at the panel end (2025-12-31) freezes the trend
   exactly where the GBM's trees freeze it. Recorded as t_index_policy.

The effect layer is re-derived from STGNN residuals through the same estimator
(effects_v2 with an injected residual frame). This is REQUIRED, not cosmetic:
per-cell lift_pct in the handler is a pure function of the effect layer, so two
arms sharing one effect layer would render byte-identical choropleths and the
website toggle would look broken.

Run order (after `spine_2026 --24` and with edges_flow.parquet built):

    uv run python -m eia_pipeline.serve.stgnn train      # ~10-15 min on MPS
    uv run python -m eia_pipeline.serve.stgnn effects    # DiD + bootstrap
    uv run python -m eia_pipeline.serve.stgnn artifacts  # cf grid + tarball inputs
"""
from __future__ import annotations

import hashlib
import json
from datetime import date as _date
from pathlib import Path

import numpy as np
import polars as pl

from ..settings import settings

EDGE_KEY = "flow"
CKPT = "data/bronze_sf/stgnn_flow.pt"
META = "data/bronze_sf/stgnn_flow.meta.json"
ART_DIR = "serve_artifacts_stgnn"
GBM_ART_DIR = "serve_artifacts"
EVENING = (16, 23)


def _ckpt_path() -> Path:
    return settings.data_dir / "bronze_sf" / "stgnn_flow.pt"


# ---------------------------------------------------------------- train


def train_and_checkpoint(edge_key: str = EDGE_KEY, seed: int = 0,
                         force: bool = False) -> Path:
    """Train the arm on the training panel and persist everything reapplication
    needs: weights, the train-split normalisation stats, the node order, and the
    t_index ceiling. The norm stats are not optional; re-deriving them on a
    different window silently rescales every input."""
    import torch

    from ..nowcast.models import tier2_stgnn as t2

    dest = _ckpt_path()
    if dest.exists() and not force:
        print(f"  checkpoint exists: {dest} (use force=True to retrain)", flush=True)
        return dest

    data = t2.build_tensors()
    ti = t2.GLOBAL_FEATS.index("t_index")
    t_index_clamp = float(data["x_g"][:, ti].max())   # raw, pre-normalisation

    res = t2.train(edge_key=edge_key, seed=seed, data=data, return_model=True)
    state = {k: v.detach().cpu() for k, v in res["model"].state_dict().items()}

    payload = {
        "state": state,
        "norm": res["norm"],
        "arch": res["arch"],
        "units": res["units"],
        "edge_key": edge_key,
        "seed": seed,
        "t_index_clamp": t_index_clamp,
        "t_index_feature_index": ti,
    }
    torch.save(payload, dest)

    meta = {k: res[k] for k in ("edges", "params", "best_step", "total_steps",
                                "train_mae", "val_mae", "test_mae", "test_rmse",
                                "test_r2", "test_bias", "n_windows",
                                "neigh_over_self", "seed")}
    meta["t_index_clamp"] = t_index_clamp
    (settings.data_dir / "bronze_sf" / "stgnn_flow.meta.json").write_text(
        json.dumps(meta, indent=2, default=float) + "\n")
    print(f"  checkpoint -> {dest}  (test MAE {res['test_mae']:.4f}, "
          f"val {res['val_mae']:.4f}, best step {res['best_step']})", flush=True)
    return dest


def load_checkpoint() -> dict:
    import torch

    return torch.load(_ckpt_path(), map_location="cpu", weights_only=False)


def rebuild_model(ckpt: dict):
    """Reconstruct the eval-mode model on the local device from a checkpoint."""
    from ..nowcast.models import tier2_stgnn as t2

    a = ckpt["arch"]
    model = t2.STGNN(a["N"], a["f_nt"], a["f_g"], a["f_s"],
                     d_emb=a["d_emb"], hidden=a["hidden"], blocks=a["blocks"])
    model.load_state_dict(ckpt["state"])
    dev = t2.device()
    model.to(dev).eval()
    return model, dev


# ---------------------------------------------------------------- tensors


def load_serve_frame() -> pl.DataFrame:
    """The 24-hour serve panel with baselines joined, at TRAINING semantics.

    One deliberate difference from model_hour_serve's own n_poi_live: the serve
    table holds a cell's LAST observed weekly count wherever its coverage row is
    missing, which is the right anti-"everything went dark" policy PAST the
    coverage horizon but silently rewrites the training window, where
    tier1_gbm.load() filled those same rows with 0 and that is what the
    checkpoint was fit on (0.47% of training cell-days; verify_serve_tensors
    caught the 56,016-position mismatch). So: held rows become 0 inside the
    span coverage data exists for, and keep the hold only beyond it.
    """
    base = settings.data_dir / "bronze_sf"
    cov_end = pl.read_parquet(
        base / "cell_week_coverage.parquet")["week_start"].max()
    df = pl.read_parquet(base / "model_hour_serve_24.parquet")
    df = df.join(pl.read_parquet(base / "rolling_baseline_serve_24.parquet"),
                 on=["unit_id", "date", "hour"], how="left")
    return df.with_columns(
        pl.when(pl.col("n_poi_live_held")
                & (pl.col("date").dt.truncate("1w").cast(pl.Date) <= cov_end))
        .then(0)
        .otherwise(pl.col("n_poi_live"))
        .fill_null(0)
        .alias("n_poi_live"),
        pl.col("us_federal_holiday").cast(pl.Int8),
    )


def build_tensors_serve() -> dict:
    """build_tensors() re-assembled over the serve window, raw (un-normalised).

    Deliberately the same construction line for line: sorted unique units,
    arithmetic _t on a dense 24-hour spine, fill_null zero, the same cyclical
    terms. verify_serve_tensors() then proves the training-window slice equals
    the training tensors exactly rather than trusting the mirroring.
    """
    from ..nowcast.models.tier2_stgnn import (GLOBAL_FEATS, NODE_TIME_FEATS,
                                              STATIC_FEATS)

    df = load_serve_frame().sort(["date", "hour", "unit_id"])
    units = df["unit_id"].unique().sort().to_list()
    uidx = {u: i for i, u in enumerate(units)}
    N = len(units)

    ts = df.select(["date", "hour"]).unique().sort(["date", "hour"])
    T = ts.height
    day0 = ts["date"].min()

    df = df.with_columns(
        pl.col("unit_id").replace_strict(uidx, return_dtype=pl.Int32).alias("_n"),
        ((pl.col("date") - pl.lit(day0)).dt.total_days() * 24
         + pl.col("hour")).cast(pl.Int32).alias("_t"),
    )
    assert df["_t"].max() == T - 1, f"timestamp index {df['_t'].max()} != {T - 1}"

    x_nt = np.zeros((T, N, len(NODE_TIME_FEATS)), dtype=np.float32)
    t_arr, n_arr = df["_t"].to_numpy(), df["_n"].to_numpy()
    for k, f in enumerate(NODE_TIME_FEATS):
        v = df[f].fill_null(strategy="zero").to_numpy().astype(np.float32)
        x_nt[t_arr, n_arr, k] = v

    g = df.unique(subset=["_t"], keep="first").sort("_t")
    x_g = np.stack([g[f].fill_null(0).to_numpy().astype(np.float32)
                    for f in GLOBAL_FEATS], axis=1)
    hh = g["hour"].to_numpy().astype(np.float32)
    dd = g["dow"].to_numpy().astype(np.float32)
    mm = g["month"].to_numpy().astype(np.float32)
    cyc = np.stack([np.sin(2 * np.pi * hh / 24), np.cos(2 * np.pi * hh / 24),
                    np.sin(2 * np.pi * dd / 7), np.cos(2 * np.pi * dd / 7),
                    np.sin(2 * np.pi * mm / 12), np.cos(2 * np.pi * mm / 12)], axis=1)
    x_g = np.concatenate([x_g, cyc.astype(np.float32)], axis=1)

    s = df.unique(subset=["_n"], keep="first").sort("_n")
    x_s = np.stack([s[f].to_numpy().astype(np.float32) for f in STATIC_FEATS], axis=1)
    br = np.deg2rad(s["bearing_venue_deg"].to_numpy().astype(np.float32))
    x_s = np.concatenate([x_s, np.stack([np.sin(br), np.cos(br)], axis=1)], axis=1)

    dates = [str(d) for d in
             ts.filter(pl.col("hour") == 0)["date"].sort().to_list()]
    return dict(x_nt=x_nt, x_g=x_g, x_s=x_s, units=units, uidx=uidx,
                T=T, N=N, dates=dates)


def verify_serve_tensors(t_serve: dict) -> None:
    """The training-window slice of the serve tensors must equal the training
    tensors EXACTLY. Bit-equality is achievable because verify(suffix='_24')
    already proved the baselines identical and the 2023-2025 spine rows are
    lifted verbatim from the gold spine, so any difference here is an assembly
    bug in build_tensors_serve, not a data difference."""
    from ..nowcast.models import tier2_stgnn as t2

    tr = t2.build_tensors()
    T0 = tr["T"]
    if tr["units"] != t_serve["units"]:
        raise RuntimeError("serve node order differs from the training node order")
    for key in ("x_nt", "x_g"):
        a, b = tr[key], t_serve[key][:T0]
        if not np.array_equal(a, b):
            bad = np.argwhere(a != b)
            raise RuntimeError(
                f"{key} differs on the training overlap at {len(bad)} positions; "
                f"first {bad[:3].tolist()}")
    if not np.array_equal(tr["x_s"], t_serve["x_s"]):
        raise RuntimeError("x_s differs from training statics")
    print(f"  OK  serve tensors match training tensors exactly on the first "
          f"{T0:,} hours (x_nt, x_g, x_s)", flush=True)


def _apply_norm(t_raw: dict, ckpt: dict) -> dict:
    """The checkpoint's train-split normalisation, with t_index clamped first."""
    out = {}
    x_g = t_raw["x_g"].copy()
    ti = ckpt["t_index_feature_index"]
    x_g[:, ti] = np.minimum(x_g[:, ti], np.float32(ckpt["t_index_clamp"]))
    raw = {"x_nt": t_raw["x_nt"], "x_g": x_g, "x_s": t_raw["x_s"]}
    for key in ("x_nt", "x_g", "x_s"):
        mu = np.asarray(ckpt["norm"][key]["mu"], dtype=np.float64)
        sd = np.asarray(ckpt["norm"][key]["sd"], dtype=np.float64)
        out[key] = ((raw[key] - mu) / sd).astype(np.float32)
    return out


# ---------------------------------------------------------------- predict


def predict_serve(ckpt: dict | None = None, t_serve: dict | None = None) -> np.ndarray:
    """[T, N] log1p(person_hours) over the serve window, every hour predicted
    exactly once. The first CONTEXT hours (all of 2023-01-02) stay NaN; the
    handler rejects that date rather than answering from a cold start."""
    import torch

    from ..nowcast.models import tier2_stgnn as t2

    ckpt = ckpt or load_checkpoint()
    t_serve = t_serve or build_tensors_serve()
    if ckpt["units"] != t_serve["units"]:
        raise RuntimeError("node order drift between checkpoint and serve tensors")

    normed = _apply_norm(t_serve, ckpt)
    model, dev = rebuild_model(ckpt)
    A = t2.adjacency(ckpt["edge_key"], t_serve["uidx"], t_serve["N"]).to(dev)
    tensors = {k: torch.from_numpy(v).to(dev) for k, v in normed.items()}
    grid = t2.predict_grid(model, tensors, A, t_serve["T"], t_serve["N"])
    return grid


# ---------------------------------------------------------------- effects


def stgnn_resid_fn(ckpt: dict | None = None):
    """A residual-frame factory for effects_v2.estimate_all(resid_fn=...).

    Predicts the TRAINING panel through predict_grid (the same estimator basis
    compare_tiers.py uses), then emits the same columns evening_residuals()
    returns. Rows without a prediction (the first CONTEXT hours) are dropped;
    NaN would otherwise poison every polars mean it touches.
    """
    import torch

    from ..nowcast.models import tier2_stgnn as t2
    from .effects_v2 import canonical_band_expr

    ckpt = ckpt or load_checkpoint()

    def _fn(df: pl.DataFrame) -> pl.DataFrame:
        data = t2.build_tensors()
        if ckpt["units"] != data["units"]:
            raise RuntimeError("node order drift between checkpoint and panel")
        normed = _apply_norm(data, ckpt)
        model, dev = rebuild_model(ckpt)
        A = t2.adjacency(ckpt["edge_key"], data["uidx"], data["N"]).to(dev)
        tensors = {k: torch.from_numpy(v).to(dev) for k, v in normed.items()}
        grid = t2.predict_grid(model, tensors, A, data["T"], data["N"])

        lo, hi = EVENING
        ev = df.filter(pl.col("hour").is_between(lo, hi))
        # arithmetic _t against the panel's own first date, same as build_tensors
        d0 = ev["date"].min()
        ev = ev.with_columns(
            pl.col("unit_id").replace_strict(data["uidx"],
                                             return_dtype=pl.Int32).alias("_n"),
            ((pl.col("date") - pl.lit(d0)).dt.total_days() * 24
             + pl.col("hour")).cast(pl.Int32).alias("_t"),
        )
        pred = grid[ev["_t"].to_numpy(), ev["_n"].to_numpy()]
        out = ev.select(
            "date", "unit_id", "dist_venue_m", "giants_home", "is_control", "y"
        ).with_columns(
            pl.Series("pred", pred.astype(np.float64)),
        ).with_columns(
            (pl.col("y") - pl.col("pred")).alias("resid"),
        ).filter(
            pl.col("pred").is_finite()
        ).with_columns(canonical_band_expr())
        n_drop = ev.height - out.height
        print(f"  stgnn residuals: {out.height:,} evening rows "
              f"({n_drop:,} dropped, no convolution context)", flush=True)
        return out

    return _fn


def build_effects(n_boot: int | None = None) -> dict:
    """effects_stgnn.json: the same DiD estimator, STGNN residuals."""
    from . import effects_v2

    kw = {} if n_boot is None else {"n_boot": n_boot}
    dest = settings.data_dir / "bronze_sf" / "effects_stgnn.json"
    return effects_v2.estimate_all(out_path=dest, resid_fn=stgnn_resid_fn(), **kw)


# ---------------------------------------------------------------- artifacts


def build_artifacts() -> Path:
    """Assemble data/serve_artifacts_stgnn/ for package.build_tarball.

    Reuses cells.npz / dates.json / unit_ids.json from the GBM artifacts
    verbatim (same geometry, same date index; the row-index contract depends on
    both). Adds cf_grid.npz, the STGNN effects.json, a two-part golden fixture,
    and a manifest with cf_source='grid'.
    """
    from . import featurespec as fs

    gbm_art = settings.data_dir / GBM_ART_DIR
    art = settings.data_dir / ART_DIR
    art.mkdir(parents=True, exist_ok=True)

    gbm_man = json.loads((gbm_art / "manifest.json").read_text())
    ckpt = load_checkpoint()
    t_serve = build_tensors_serve()
    verify_serve_tensors(t_serve)

    units = json.loads((gbm_art / "unit_ids.json").read_text())
    if units != t_serve["units"]:
        raise RuntimeError("unit_ids.json does not match the serve node order")

    dates = json.loads((gbm_art / "dates.json").read_text())
    if [d["date"] for d in dates] != t_serve["dates"]:
        raise RuntimeError("dates.json does not match the serve date index")

    print("  predicting the serve grid ...", flush=True)
    grid = predict_serve(ckpt, t_serve)          # [T, N] with T = 1365*24

    n_dates, n_cells = len(dates), len(units)
    lo, hi = EVENING
    n_h = hi - lo + 1
    ev = grid.reshape(n_dates, 24, n_cells)[:, lo:hi + 1, :]   # [D, 8, N]
    cf_flat = np.ascontiguousarray(ev, dtype=np.float32).ravel()

    # Row-index contract, asserted by reconstruction rather than trusted:
    # row = ((date_idx * n_h) + (hour - lo)) * n_cells + (unit_code - 1)
    rng = np.random.default_rng(0)
    di = rng.integers(0, n_dates, 2000)
    hr = rng.integers(lo, hi + 1, 2000)
    uc = rng.integers(1, n_cells + 1, 2000)
    rows = (di * n_h + (hr - lo)) * n_cells + (uc - 1)
    direct = grid[di * 24 + hr, uc - 1]
    both_nan = np.isnan(cf_flat[rows]) & np.isnan(direct)
    if not np.array_equal(cf_flat[rows][~both_nan], direct[~both_nan]):
        raise RuntimeError("row-index reconstruction does not match the grid")

    # NaN only where there is no convolution context: date 0, nowhere else.
    nan_rows = np.isnan(cf_flat).reshape(n_dates, -1).any(axis=1)
    if nan_rows[0] is np.False_ or nan_rows[1:].any():
        raise RuntimeError(
            f"unexpected NaN layout: dates with NaN = {np.where(nan_rows)[0][:5]}")

    np.savez_compressed(art / "cf_grid.npz", cf_log=cf_flat)

    eff_path = settings.data_dir / "bronze_sf" / "effects_stgnn.json"
    if not eff_path.exists():
        raise RuntimeError("effects_stgnn.json missing; run `stgnn effects` first")
    eff = json.loads(eff_path.read_text())
    (art / "effects.json").write_text(json.dumps(eff, indent=2) + "\n")

    for name in ("cells.npz", "dates.json", "unit_ids.json"):
        (art / name).write_bytes((gbm_art / name).read_bytes())

    # Golden fixture, two halves. Index half: 256 finite rows, exact float32
    # equality on the lookup after the handler recomputes the row index from
    # components. Effect half: 256 (dist, band, attendance, night) tuples
    # through fs.effect_log, pinning the half of the pipeline that still runs
    # real code in the container.
    finite = np.where(np.isfinite(cf_flat))[0]
    pick = rng.choice(finite, 256, replace=False)
    g_di = (pick // n_cells) // n_h
    g_hr = (pick // n_cells) % n_h + lo
    g_uc = pick % n_cells + 1

    e_dist = rng.uniform(50, 11000, 256)
    e_att = rng.uniform(20000, 42000, 256)
    e_night = rng.integers(0, 2, 256).astype(float)
    e_band = np.array([fs.band_of(float(d), eff["bands"]) for d in e_dist],
                      dtype=object)
    e_log = fs.effect_log(e_dist, e_band, e_att, e_night, eff)
    np.savez_compressed(
        art / "golden.npz",
        row=pick.astype(np.int64), date_idx=g_di.astype(np.int64),
        hour=g_hr.astype(np.int64), unit_code=g_uc.astype(np.int64),
        cf_log=cf_flat[pick],
        eff_dist=e_dist, eff_band=e_band.astype(str), eff_att=e_att,
        eff_night=e_night, eff_log=e_log,
    )

    meta = json.loads((settings.data_dir / "bronze_sf" /
                       "stgnn_flow.meta.json").read_text())
    fs_sha = hashlib.sha256(Path(fs.__file__).read_bytes()).hexdigest()
    man = {
        "schema": "oracle-ripple-artifacts/1",
        "cf_source": "grid",
        "model_family": "stgnn",
        "edge_key": ckpt["edge_key"],
        "seed": ckpt["seed"],
        # date 0 has no convolution context and is rejected, so the honest
        # window starts one day after the GBM's
        "serve_window": [str(_date.fromisoformat(gbm_man["serve_window"][0])
                             .replace(day=3)), gbm_man["serve_window"][1]],
        "training_window": gbm_man["training_window"],
        "observed_panel_through": gbm_man["observed_panel_through"],
        "evening_hours": list(EVENING),
        "n_cells": n_cells,
        "n_dates": n_dates,
        "n_rows": int(cf_flat.size),
        "row_index": gbm_man["row_index"],
        "featurespec_sha256": fs_sha,
        "t_index_policy": (
            f"raw t_index clamped at {ckpt['t_index_clamp']:.0f} (2025-12-31) "
            "before train-split normalisation; the model's linear input has no "
            "saturation, so unclamped 2026 dates would extrapolate the panel's "
            "coverage drift ~2.7x past anything the fit ever saw"),
        "served_variant": "train_split_checkpoint",
        "checkpoint_metrics": meta,
        "golden_n": 256,
    }
    (art / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")

    print(f"  artifacts -> {art}  (cf_grid {cf_flat.nbytes / 1e6:.1f} MB, "
          f"{len(finite):,}/{cf_flat.size:,} finite rows)", flush=True)
    return art


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--force", action="store_true")
    e = sub.add_parser("effects")
    e.add_argument("--n-boot", type=int, default=None)
    sub.add_parser("artifacts")
    n = ap.parse_args()

    if n.cmd == "train":
        train_and_checkpoint(seed=n.seed, force=n.force)
    elif n.cmd == "effects":
        build_effects(n_boot=n.n_boot)
    elif n.cmd == "artifacts":
        build_artifacts()
