# Report figures

Rendered by `src/eia_pipeline/eda/charts.py`. Regenerate all of them with:

```bash
make figures
```

or a subset with `uv run python -m eia_pipeline.eda.charts panel_coverage covariate_balance`.

Every figure is drawn from a file already in the repo — nothing here re-runs a model or
reaches S3 — so a chart and the table it accompanies cannot drift apart. To change a
number, change it upstream and re-render.

PNG, 200 dpi. Each carries a dot legend keying every colour it uses. Sources are not
printed on the figures — the table below is the record of what each one is drawn from.

## The figures

| # | File | The claim it makes | Drawn from |
|---|---|---|---|
| 1 | `fig01_panel_coverage.png` | The Advan panel thins by a third across 2023-2025, and measured activity falls with it — levels are not comparable across years | `data/bronze_sf/model_hour.parquet` |
| 2 | `fig02_bart_calibration.png` | Pre-game transit uplift scales with the gate: r = 0.69, ~54 net arrivals per 1,000 fans | `data/gold/bart_attendance_calib_2024.parquet` |
| 3 | `fig03_diurnal_profile.png` | Day and night games each peak at their own first pitch, and the gap decays with distance | `data/bronze_sf/model_hour.parquet` |
| 4 | `fig04_covariate_balance.png` | Game days are warmer and drier than control days — why weather is in the counterfactual and not waved away | `data/bronze_sf/model_hour.parquet` |
| 5 | `fig05_distance_decay.png` | Each event moves its own inner ring and not the other venue's; the placebo panels are the check | `data/bronze_sf/tier3_crossover.json` |
| 6 | `fig06_feature_ablation.png` | Each Tier 1 feature block lowers error; the multi-depth baseline is what takes test MAE below 1.0 | Tier 1 GBM ablation (docs/PIPELINE.md) |
| 7 | `fig07_edge_ablation_seeds.png` | Seed noise is wider than the gap between edge arms — the graph structure is not identifiable here | `data/bronze_sf/tier2_ablation_seeds.json` |
| 8 | `fig08_dollars_by_band.png` | $85.5k attributable taxable food spend per game, and which band each dollar comes from | nowcast.game_dollars (docs/PIPELINE.md) |
| 9 | `fig09_target_distribution.png` | 27.5% zeros and skew 6.2 raw; log1p is why everything is modelled in logs | `eda.profile.target` |
| 10 | `fig10_missingness.png` | Three quarters of the nulls are the design; what is left is one doubleheader and the weather series | `eda.profile.missingness` |
| 11 | `fig11_covariate_correlation.png` | Net of hour-of-day the hourly weather series collapse from ~0.15 to ~0.01 — they were the clock | `eda.profile.correlations` |
| 12 | `fig12_split_drift.png` | Train sits in a denser panel than test: mean 497 → 366 person-hours, zero share 24% → 35% | `eda.profile.splits` |

Figures 9 to 12 are the profiling layer. They share every number with
[docs/07_data_profile.md](../../07_data_profile.md), which is generated from the same
functions by `make profile` — the prose and the pictures cannot disagree.

Figures 6 and 8 carry their numbers as literals in `charts.py`, because the runs that
produced them are not re-runnable from local files. They match the ablation and dollars
numbers recorded in `docs/PIPELINE.md`. If those are re-run, update the literals with them.

## Suggested order for a talk

9, 10, 1, 12 are the data section — what the target looks like, what is missing, how the
panel thins, and what that does to the splits. Then 4 and 3 set up the identification
problem, 5 is the causal result, 11, 6 and 7 are the modelling honesty, and 8 is the
dollar figure. 2 belongs wherever the BART calibration gets told, which in a short talk is
usually nowhere.
