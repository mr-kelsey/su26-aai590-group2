"""Tier 2 edge ablation over the current panel.

One run, every arm, so the percentages we quote come from a single experiment.
The previous numbers in the docs mixed two runs: flow and distance came from the
step-checkpointed run and contiguity from an earlier per-epoch one that never
covered the other arms, so the three figures had never coexisted.

`flow` needs edges_flow.parquet, which is built from VISITOR_HOME_CBGS on S3.
Arms whose edge file is missing are skipped and named in the output rather than
silently dropped.

    uv run python scripts/run_ablation.py [out.json] [arm ...]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from eia_pipeline.nowcast.models import tier2_stgnn as t2

ARMS = ["none", "contiguity", "distance", "flow"]


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1
               else "data/bronze_sf/tier2_ablation_v5.json")
    arms = sys.argv[2:] or ARMS

    runnable, skipped = [], []
    for a in arms:
        if a == "none" or Path(t2.EDGE_FILES[a]).exists():
            runnable.append(a)
        else:
            skipped.append(a)
    if skipped:
        print(f"SKIPPING (edge file absent): {', '.join(skipped)}", flush=True)

    # Built once and shared: every arm must see identical features and splits or
    # the comparison measures the panel rather than the graph.
    data = t2.build_tensors()
    print(f"panel: {data['N']} nodes x {data['T']} hours", flush=True)

    results = []
    for arm in runnable:
        print(f"\n=== {arm} ===", flush=True)
        t0 = time.time()
        r = t2.train(edge_key=arm, data=data, verbose=True)
        r.pop("model", None)
        r.pop("tensors", None)
        r.pop("A", None)
        r["secs"] = time.time() - t0
        r["n_nodes"] = data["N"]
        results.append(r)
        print(f"  train {r['train_mae']:.4f}  val {r['val_mae']:.4f}  "
              f"test {r['test_mae']:.4f}  R2 {r['test_r2']:.4f}  "
              f"best step {r['best_step']}/{r['total_steps']}  "
              f"{r['secs']:.0f}s", flush=True)
        out.write_text(json.dumps(results, indent=2))

    base = next((r["test_mae"] for r in results if r["edges"] == "none"), None)
    if base:
        print("\n=== vs no graph ===", flush=True)
        for r in results:
            print(f"  {r['edges']:<12} test MAE {r['test_mae']:.4f}  "
                  f"{100 * (base - r['test_mae']) / base:+.2f}%", flush=True)
    if skipped:
        print(f"\nNOT RUN: {', '.join(skipped)}", flush=True)
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
