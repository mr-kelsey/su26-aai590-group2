"""Tier 2 edge ablation replicated across seeds.

A single run cannot rank the edge families. The spread across seeds is about
0.021 on test MAE and the gaps between edge families are 0.015 to 0.041, so one
run per arm reads the seed and calls it the graph. Every Tier 2 number has to be
a distribution, and the arms have to be compared paired by seed, because a seed
that suits one arm suits all of them.

We run the first arm twice at the same seed as a control, which separates
run-to-run variance from seed variance. It came back at 0.0011 against a seed
spread of 0.021, so the model is effectively deterministic and seeds are the
thing being measured. Earlier runs that looked like they disagreed at a fixed
seed were separated by edits to spatial_units and features rather than by
anything random, so rebuild the panel before blaming the optimiser.

    uv run python scripts/run_ablation_seeds.py [out.json] [n_seeds]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

from eia_pipeline.nowcast.models import tier2_stgnn as t2

ARMS = ["none", "contiguity", "distance", "flow"]


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1
               else "data/bronze_sf/tier2_ablation_seeds.json")
    n_seeds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    seeds = list(range(n_seeds))

    arms = [a for a in ARMS if a == "none" or Path(t2.EDGE_FILES[a]).exists()]
    skipped = [a for a in ARMS if a not in arms]
    if skipped:
        print(f"SKIPPING (edge file absent): {', '.join(skipped)}", flush=True)

    data = t2.build_tensors()
    print(f"panel: {data['N']} nodes x {data['T']} hours", flush=True)

    jobs = [(a, s) for a in arms for s in seeds]
    jobs.append((arms[0], seeds[0]))  # duplicate: run-to-run variance control
    print(f"{len(jobs)} runs: {len(arms)} arms x {n_seeds} seeds + 1 repeat\n", flush=True)

    results = []
    for k, (arm, seed) in enumerate(jobs, 1):
        tag = f"{arm} seed={seed}" + (" [REPEAT]" if k == len(jobs) else "")
        print(f"=== [{k}/{len(jobs)}] {tag} ===", flush=True)
        t0 = time.time()
        r = t2.train(edge_key=arm, seed=seed, data=data, verbose=True)
        for key in ("model", "tensors", "A"):
            r.pop(key, None)
        r["secs"] = time.time() - t0
        r["seed"] = seed
        r["repeat"] = (k == len(jobs))
        results.append(r)
        print(f"  train {r['train_mae']:.4f}  val {r['val_mae']:.4f}  "
              f"test {r['test_mae']:.4f}  R2 {r['test_r2']:.4f}  "
              f"best {r['best_step']}/{r['total_steps']}  {r['secs']:.0f}s\n", flush=True)
        out.write_text(json.dumps(results, indent=2))

    print("=== test MAE by arm (mean +/- sd over seeds) ===", flush=True)
    summ = {}
    for arm in arms:
        v = np.array([r["test_mae"] for r in results
                      if r["edges"] == arm and not r["repeat"]])
        summ[arm] = {"mean": float(v.mean()), "sd": float(v.std(ddof=1)) if v.size > 1 else 0.0,
                     "n": int(v.size), "values": v.tolist()}
        print(f"  {arm:<12} {v.mean():.4f} +/- {summ[arm]['sd']:.4f}  "
              f"[{', '.join(f'{x:.4f}' for x in v)}]", flush=True)

    base = summ[arms[0]]["mean"] if arms[0] == "none" else None
    if base:
        pooled = float(np.mean([s["sd"] for s in summ.values()]))
        print(f"\n=== vs no graph, against pooled seed sd {pooled:.4f} ===", flush=True)
        for arm in arms[1:]:
            d = base - summ[arm]["mean"]
            print(f"  {arm:<12} {100 * d / base:+.2f}%  "
                  f"({d / pooled:+.2f} sd -- {'resolvable' if abs(d) > 2 * pooled else 'NOT resolvable'})",
                  flush=True)

    rep = [r for r in results if r["edges"] == arms[0] and r["seed"] == seeds[0]]
    if len(rep) == 2:
        gap = abs(rep[0]["test_mae"] - rep[1]["test_mae"])
        print(f"\n=== same-seed repeat: {rep[0]['test_mae']:.4f} vs "
              f"{rep[1]['test_mae']:.4f}  (gap {gap:.4f}) ===", flush=True)
        print("  bitwise reproducible" if gap == 0 else
              "  NOT reproducible at a fixed seed -- kernels are nondeterministic",
              flush=True)

    out.write_text(json.dumps({"runs": results, "summary": summ}, indent=2))
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()
