# Citywide spatiotemporal nowcast: pipeline map

What runs, in what order, what each stage produces, and the one decision per stage
that would be wrong to change. Design rationale lives in
`docs/superpowers/specs/2026-07-31-citywide-spend-nowcast-design.md`; this file is
the operational map.

**Scope.** Everything here is new work on `feature/citywide-spatiotemporal-nowcast`.
It is additive to the team repo: `io.py`, `settings.py` and `nowcast/disaggregate.py`
are byte-identical across both repos and are *reused, not modified*.

---

## Run order

```
 bronze (S3)
     │
 1.  ingest/advan_bronze          poi_dim · daily/ · hourly/
     │
 2.  transform/spatial_units      poi_cell · cell_dim            452 cells
     │
 3.  transform/panel              cell_hour · cell_day · cell_week_coverage
     │
 4.  transform/features           model_hour · rolling_baseline  11.9M rows
     │           │
     │      5.  transform/graph   edges_{contiguity,distance,flow}
     │           │
     ├───────────┴──────────────────────────────┐
     │                                          │
 6.  models/tier1_gbm  (GBM)              7.  models/tier2_stgnn  (STGNN)
 8.  models/deploy_gbm (forecaster)             │
     │                                          │
 9.  nowcast/effects        ──────────► 10. nowcast/tier3_venue
     │                                          (crossover)
 11. nowcast/game_dollars   ◄── nowcast/disaggregate (pre-existing)
```

Stages 1–5 are build-once. Stages 6–11 read the landed Parquet and can be re-run
freely. All outputs land under `data/bronze_sf/` (gitignored).

---

## 1 · `ingest/advan_bronze.py`

Citywide SF presence from raw Advan weekly patterns.

| | |
|---|---|
| **Entry** | `poi_dimension()`, `explode_daily()`, `explode_hourly()` |
| **Reads** | `s3://<bucket>/bronze/advan_weekly_patterns/*.parquet` |
| **Writes** | `poi_dim.parquet` · `daily/year=*.parquet` · `hourly/year=*.parquet` |
| **Scale** | 17,826 SF POIs · 334 weeks · 30.4M POI-days · 154.5M POI-hours |
| **Runtime** | ~140 s total |

**Do not change:** the array to calendar mapping. Weeks start Monday, so for 1-based
index `i`, `day = start + (i-1)//24`, `hour = (i-1)%24`. `verify_alignment()`
proves this by reproducing `silver/occupancy_poi_hour` **exactly** (351,207 rows,
100% value equality, zero rows on either side only). An off-by-one here shifts every
downstream effect by a day and still looks entirely plausible. **Run the gate first.**

**Unit trap:** `VISITS_BY_DAY` (7 elements) is *visits*; `VISITS_BY_EACH_HOUR`
(168 elements) is *person-hours*, because a visitor is counted in every hour they
are present, roughly 4x. They do not reconcile and must never be mixed.

## 2 · `transform/spatial_units.py`

POI → `unit_id`. **The pluggable seam of the whole design.** Everything downstream
joins on `unit_id` and is invariant to what a unit is, so swapping 500 m cells,
census tracts or DataSF neighbourhoods touches only this module.

| | |
|---|---|
| **Entry** | `build()` |
| **Writes** | `poi_cell.parquet` · `cell_dim.parquet` |
| **Result** | **452 cells** (≥10 POIs), 14,467 POIs retained = 81.9% |

**Do not change:** the 250 m cell size, without re-measuring. It was chosen on the
spatial correlation function, not convenience. Hourly adjacent-cell residual
correlation is 0.445 against a correlation length of 750 m–1 km, so cells sit at
roughly 1/4 of that: informative but not redundant. At 1 km neighbours correlate 0.154
and the graph is informationally disconnected.

**Note:** Oracle Park itself falls below the 10-POI floor and is *excluded*. That is
desirable, because the venue was 73.6% of near-field activity in the gold ring-1 work, so
including it makes "activity concentrates at the ballpark" tautological.

## 3 · `transform/panel.py`

| | |
|---|---|
| **Entry** | `build_cell_hour()`, `build_cell_day()`, `build_cell_week_coverage()` |
| **Writes** | `cell_hour.parquet` (11,878,560 = 452 × 1,095 × 24, **72.5% non-zero**) · `cell_day.parquet` · `cell_week_coverage.parquet` |

**Do not change:** the dense spine + zero-fill in `cell_hour`. Bronze stores non-zero
rows only, so a missing cell-hour is an *unwritten zero*, not a null. Aggregating
without the spine computes every mean over the wrong denominator and silently
inflates it.

**Why coverage is weekly:** `n_poi_reporting` at hour grain leaks the target
(`person_hours = 0` implies it is 0). `n_poi_live`, the count of distinct POIs
reporting across an ISO week, tracks the vendor drift without encoding any single hour's outcome.

## 4 · `transform/features.py`

| | |
|---|---|
| **Entry** | `build()`, then `build_rolling_baseline_multi()` |
| **Writes** | `model_hour.parquet` (11.9M rows) · `rolling_baseline.parquet` |

Covariates are joined from `gold/gnn_time_hour` rather than reassembled, so **our
train/val/test splits are identical to the team's GNN work** and the numbers are
directly comparable.

**Two control definitions, deliberately:** `clean_control` (as shipped, 828 days)
and `clean_control_strict` (581 days, also excluding Chase/Moscone/citywide/street
fairs). Both are carried so the sensitivity is a measured result. It came out at
-0.3%, matching gold QA's ring-1 figure.

**Do not change:** the rolling baselines are trailing-only and built from control
days only. Trailing-only because the team's v0 looks both directions, which is fine
for measurement but would leak across the split boundary. Control-days-only because
feeding recent *raw* activity means that during a game the model predicts the
inflated level and the residual, which *is* the measurement, collapses to zero.

Three depths (`base_k2`, `base_k4`, `base_cap120`) because a single k=8 spanned a
median 112 calendar days on game days (max 203); RMSE has an interior optimum near
k=3-4 and combining depths beats any single one.

## 5 · `transform/graph.py`

| | |
|---|---|
| **Entry** | `build_contiguity()`, `build_distance()`, `build_flow()` |
| **Writes** | `edges_contiguity.parquet` (2,100) · `edges_distance.parquet` (10,598) · `edges_flow.parquet` (3,489) |

Distance edges are weighted by the *measured* correlation function and scaled by
pair density, because correlation among dense downtown pairs (0.202) is ~4× that
among thin outer pairs (0.055), so a uniform kernel is mis-specified.

## 6–8 · The three models

| module | trained on | event features | residual causal? | forecasts events? |
|---|---|---|---|---|
| `tier1_gbm` | control hours | no | **yes** | no |
| `tier2_stgnn` | control hours | no | yes | no |
| `deploy_gbm` | **all** hours | **yes** | **no** | **yes** |

**The measurement/deployment split is not fastidiousness.** A model that has seen
event hours has partly fitted the event, so its residual understates the effect.
A model without event features cannot forecast one. Mixing them silently corrupts
either the forecast or the causal claim.

**Tier 1** (`fit()`) is the benchmark: test MAE **0.9180**, R² 0.7569, beating the
naive cell-hour-of-week baseline by 32%.

**Tier 2** (`train()`) is STGCN-style, *not* DCRNN, because lag-1 spatial
correlation measured at or *below* contemporaneous, so there is no travelling wave
for diffusion convolution to model. Best config: test MAE 1.0689. **It loses to
Tier 1 by 16%**, which the spec anticipated as a reportable finding. The graph
itself does help within the STGNN family (flow -1.9%, distance -1.6% vs no-graph;
raw contiguity *hurts* +1.6%).

**`deploy_gbm`** is validated on held-out **game** hours, not control hours. A
forecaster scoring well on quiet Tuesdays has demonstrated nothing. Run it with
`near_weight=8`: the near field is ~1% of rows, and unweighted the model
under-fits it badly (0-500 m MAE 1.0116 to 0.9608 with weighting; 25 overshoots).

**Even so, at 0–500 m it loses to the free `composition_benchmark()` by 7.9%**
(0.9608 vs 0.8906) in a fair comparison, and only there. It wins 500 m to 1 km,
1 to 2 km and 2 to 4 km. So: **composition for near-field forecasts, `deploy_gbm`
elsewhere.** See *Known gaps* for how that was established.

## 9 · `nowcast/effects.py`

| | |
|---|---|
| **Entry** | `fit_full_control_model()` → `day_band_residuals()` → `estimate()`, `placebo()` |

Effects are a **difference-in-differences on residuals**: game-hour residual minus
contemporaneous control-hour residual. Differencing cancels the vendor drift.

**Do not change:** inference clusters at the **day**. Cell-hours within a day are
heavily correlated, and naive cell-hour t-statistics overstate significance by ~3x
(the 0-500 m band reads t=7.2 unclustered against z=+2.2 versus placebo).

Result, 246 games vs 581 controls, 2,000-draw day-clustered bootstrap:

| band | effect | 95% CI | p |
|---|---|---|---|
| 0–500 m | **+44.6%** | [+39.8, +49.9] | <0.0001 |
| 500 m–1 km | **+14.8%** | [+12.2, +17.4] | <0.0001 |
| 1–2 km | **+4.5%** | [+3.3, +5.7] | <0.0001 |
| 2–4 km | +1.2% | [+0.4, +2.0] | 0.002 |
| >4 km | +0.2% | [−0.6, +1.1] | 0.597 |

The rest of the city does **not** fall, so the near-field lift is net new activity
rather than spend relocated within SF.

## 10 · `nowcast/tier3_venue.py`

| | |
|---|---|
| **Entry** | `crossover()` |

Chase Center is 1,188 m away with 211 event days disjoint from Giants games (45
shared days **dropped**, not controlled). Their 0–500 m rings share zero cells.

| | around Oracle | around Chase |
|---|---|---|
| Giants only | **+43.8%** | −11.5% (ns) |
| Chase only | +1.3% (ns) | **+648.7%** |

Diagonal huge, off-diagonal null: **the effect follows the venue, not the
neighbourhood.** A held-out *time* split could never show this, because a model
that had merely memorised where the ballpark is would pass one.

## 11 · `nowcast/game_dollars.py`

| | |
|---|---|
| **Entry** | `per_game()`; uses pre-existing `nowcast/disaggregate.py` |
| **Result** | **$85,507 per game**, 95% [$59.6k, $110.9k] · $21.0M per 246-game season |

Quarterly CDTFA C08 → daily via Denton, reconciling **exactly** (max relative error
4.8e-15 over 12 quarters / $14.18B).

**Do not change** these three, each of which would inflate the number:
1. the evening share (34.5%), because effects are estimated on hours 16-23 only;
2. the attributable fraction is **e/(1+e), not e**, since observed already contains the effect;
3. the >4 km band enters as **0**, not its noisy point estimate.

**Velocity drifts** (+21% $/visit over the window, almost all price not volume).
Denton absorbs this because it reconciles each quarter exactly, but velocity
therefore **cannot** be read as a structural parameter, and nothing may be
extrapolated beyond the anchor window.

---

## Known gaps

- **`deploy_gbm` near-field: RESOLVED, and it is a finding.** Both candidate
  causes were tested. The test-set leak was negligible: rebuilding the benchmark
  from train-only band effects (41.4% vs the full-window 44.6% at 0-500 m) moved
  its MAE only 0.8842 to 0.8906. Stratum starvation was real: `near_weight=8`
  improved the model 1.0116 to 0.9608, with 25 overshooting (1.0025), so there is
  an interior optimum. **But in a fair comparison the composition still wins the
  near field by 7.9%**, and only there. The trained model wins 500 m to 1 km
  (+0.3%), 1 to 2 km (+2.2%) and 2 to 4 km (+0.7%). A structural estimate that spends
  all its evidence on one band beats a global learner that must treat those 5
  cells (1.1% of rows) as rounding error. Practical consequence: **use the
  composition for near-field forecasts and `deploy_gbm` elsewhere.**
- **No tests** on any of this yet. The three worth writing are the invariants that
  would silently corrupt everything: day alignment, disaggregation reconciliation,
  panel shape.
- **Tier 2 capacity question closed** by the run-1 curves (train 0.847 vs val 1.053
  implies overfitting, not a capacity ceiling), so more capacity would not help.
