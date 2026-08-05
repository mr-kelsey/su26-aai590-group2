"""Score Tier 1 and Tier 2 on identical cell-hours.

The two tiers were never comparable as reported. Tier 1 evaluates every control
cell-hour in the test split exactly once. Tier 2 evaluates inside overlapping 48h
windows at stride 6, so each hour enters its average about four times and hours
near a split edge may not be covered at all. Any "Tier 2 loses to Tier 1 by X%"
was comparing two averages taken over different populations.

We score both on Tier 2's `predict_grid` basis, which predicts every hour exactly
once with a full convolution context behind it. The matched set is the
intersection, meaning test-split control hours where the grid is defined, and
both models are scored on those rows and nothing else.

    uv run python scripts/compare_tiers.py [edge_arm] [out.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

from eia_pipeline.nowcast.models import tier1_gbm as t1
from eia_pipeline.nowcast.models import tier2_stgnn as t2

CONTROL = "clean_control_strict"


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    err = p - y
    return {
        "n": int(y.size),
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err ** 2))),
        "bias": float(np.mean(err)),
        "r2": float(1 - np.sum(err ** 2) / np.sum((y - y.mean()) ** 2)),
    }


def main() -> None:
    arm = sys.argv[1] if len(sys.argv) > 1 else "flow"
    out = Path(sys.argv[2] if len(sys.argv) > 2
               else "data/bronze_sf/tier_comparison.json")

    print(f"=== Tier 2 ({arm}) ===", flush=True)
    data = t2.build_tensors()
    r2res = t2.train(edge_key=arm, data=data, return_model=True, verbose=True)
    grid = t2.predict_grid(r2res["model"], r2res["tensors"], r2res["A"],
                           data["T"], data["N"])

    # Long frame of every grid cell-hour Tier 2 actually predicted.
    ts = data["ts"].with_row_index("_t")
    units = pl.DataFrame({"_n": np.arange(data["N"], dtype=np.int32),
                          "unit_id": data["units"]})
    tt, nn = np.meshgrid(np.arange(data["T"]), np.arange(data["N"]), indexing="ij")
    long = pl.DataFrame({
        "_t": tt.ravel().astype(np.int32),
        "_n": nn.ravel().astype(np.int32),
        "pred_t2": grid.ravel(),
    }).filter(pl.col("pred_t2").is_not_nan())
    long = (long.join(ts, on="_t", how="left")
                .join(units, on="_n", how="left")
                .select(["unit_id", "date", "hour", "pred_t2"]))

    print("\n=== Tier 1 ===", flush=True)
    t1res = t1.fit(control_col=CONTROL)
    df = t1.load(CONTROL)
    Xall, _ = t1._xy(df)
    df = df.with_columns(pl.Series("pred_t1", t1res["_model"].predict(Xall)))

    # Matched set: test-split control hours that Tier 2 actually predicted.
    matched = (df.filter(pl.col("is_control") & (pl.col("split") == "test"))
                 .join(long, on=["unit_id", "date", "hour"], how="inner"))
    y = matched["y"].to_numpy()

    res = {
        "arm": arm,
        "control": CONTROL,
        "matched_rows": matched.height,
        "tier1_own_basis": t1res["test"],
        "tier2_window_basis": {"mae": r2res["test_mae"], "rmse": r2res["test_rmse"],
                               "r2": r2res["test_r2"], "bias": r2res["test_bias"]},
        "tier1_matched": metrics(y, matched["pred_t1"].to_numpy()),
        "tier2_matched": metrics(y, matched["pred_t2"].to_numpy()),
    }
    g = res["tier2_matched"]["mae"] / res["tier1_matched"]["mae"] - 1
    res["tier2_vs_tier1_mae_pct"] = 100 * g

    print(f"\n=== matched on {matched.height:,} test control cell-hours ===")
    print(f"  Tier 1  MAE {res['tier1_matched']['mae']:.4f}  "
          f"RMSE {res['tier1_matched']['rmse']:.4f}  "
          f"R2 {res['tier1_matched']['r2']:.4f}")
    print(f"  Tier 2  MAE {res['tier2_matched']['mae']:.4f}  "
          f"RMSE {res['tier2_matched']['rmse']:.4f}  "
          f"R2 {res['tier2_matched']['r2']:.4f}")
    print(f"  Tier 2 is {g * 100:+.2f}% on MAE vs Tier 1 (positive = worse)")
    print("\n  for contrast, the two as previously reported:")
    print(f"    Tier 1 own basis      MAE {t1res['test']['mae']:.4f}")
    print(f"    Tier 2 window basis   MAE {r2res['test_mae']:.4f}")

    out.write_text(json.dumps(res, indent=2))
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
