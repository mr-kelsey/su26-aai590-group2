# Design — Citywide spatiotemporal spend nowcast (bronze-native)

**Date:** 2026-07-31 (rev. 2 — supersedes the rev. 1 aggregate-only design)
**Status:** proposed design, pending approval
**Deadline:** 2026-08-10
**Branch:** separate feature branch; does not touch the team `silver/`/`gold/` contract
**Relationship to existing work:** independent of the gold GNN v1 slot. Implements
`docs/02` ladder steps 1 and 4 (temporal disaggregation → state-space attribution),
citywide, with an explicit spatial layer. Reverses no decision in `docs/03`.

---

## 1. Purpose

Produce **daily San Francisco food-service dollars**, reconciled exactly to the
quarterly CDTFA anchor, plus an **hourly spatiotemporal model of where activity sits
in the city** — so a Giants home game can be read as both a dollar figure and a
visible redistribution of the city's economic activity, each with intervals.

## 2. What changed from rev. 1

Rev. 1 collapsed geography to a single citywide series to buy displacement-netting.
That is still the right move for the *dollar* claim, but it discarded the spatial
dimension. Rev. 2 keeps the citywide aggregate **and** adds a spatial substrate
underneath it, because measurement showed the substrate is far better supported than
assumed: **452 grid cells at 250m carry ≥10 POIs each and hold 81.9% of the city's
POIs** — PEMS-BAY scale (325 sensors). The small-N objection to a graph model
dissolves at this resolution.

The decomposition that unifies the two:

```
activity(unit, hour) = Total(hour) × Share(unit, hour)
```

`Total` answers *was activity created* — the netting result, anchored to CDTFA.
`Share` answers *where did it move* — the heat map. Modelled separately, recombined
exactly. Shares are always derived by renormalising predicted levels, never fitted as
independent per-unit share models, or they will not sum to one.

## 3. Why bronze, not gold

| axis | gold/GNN path | bronze citywide path |
|---|---|---|
| geography | 5km disc — neither the city nor a clean subset | SF panel = anchor geography exactly |
| hourly POIs | 12,095, capped at 5km | **~16,400–17,600, citywide** |
| density | node-hour target ~85% missing | dense weekly rows, 168-element hourly array |
| graph | 5,842 nodes, 8-NN edges 35–92m, ring1↔ring2 = 83 edges | 452 nodes, contiguity + flow edges |

**Geographic congruence is load-bearing.** Per the CDTFA README, *"SF is a
consolidated city-county (FIPS 06075), so county grain = city grain."* A citywide POI
panel matches the anchor exactly, making the velocity coefficient well-posed rather
than approximately-posed.

**Displacement-netting is structural.** If game-day activity merely relocates within
San Francisco, a citywide indicator does not move, so the disaggregation cannot
attribute new dollars to it. Under the ring framing this needed a separate correction.

**Why the gold graph was rejected.** Measured 2026-07-31: spatial edges average
35–92m, so propagating from the ballpark to 2.5km needs dozens of hops against a 2–4
layer budget; 5,352 of 5,842 nodes (92%) sit in ring 4, which the team's own event
study reports as effectively null (ring-5 lift 0.09%, r≈0.13); ring 1 is 37 POIs of
which the ballpark alone is **73.6%** of visitor-hours. The idea was sound, the grain
was wrong.

## 4. Verified data landscape

All measured against `s3://aai-590-group2-capstone` on 2026-07-31. Recorded so
implementation does not re-derive it.

### Presence — `bronze/advan_weekly_patterns/`

| property | value |
|---|---|
| SF POIs (`CITY ILIKE 'San Francisco'`) | **17,826** |
| Weeks | **334** — 2020-01-06 → 2026-05-25 |
| `VISITS_BY_DAY` | VARCHAR JSON, **7 elements**, reconciles to `VISIT_COUNTS` |
| `VISITS_BY_EACH_HOUR` | VARCHAR JSON, **168 elements** (7×24) |
| hourly explode cost | 1 week = 1.69M cells / 421k non-zero / 10,043 POIs in ~17s |
| full citywide hourly | **≈141M non-zero POI-hours**, ~15–30 min one-time |

**Unit trap.** The hourly array does *not* reconcile to `VISIT_COUNTS` (mean absolute
gap 3,218 on the tested week). Advan counts a visitor in every hour they are present,
so the hourly array sums to **person-hours** while `VISITS_BY_DAY` sums to **visits**.
Roughly a 4× ratio. Never mix the two units.

### Spatial substrate — SF POIs per grid cell

| cell size | cells | ≥5 POIs | ≥10 POIs | ≥20 POIs | share of POIs in ≥10 cells |
|---|---|---|---|---|---|
| **250m** | 1,484 | 695 | **452** | 274 | **81.9%** |
| 500m | 478 | 369 | 281 | 198 | 95.3% |
| 1km | 146 | 127 | 118 | 102 | 99.5% |

Food/drink only at 250m: 803 cells, of which **161** carry ≥10 food POIs (4,548 food
POIs total). Food is too thin for a food-specific model at 250m — see §7.

### Cell resolution is chosen by measurement, not judgment

Measured on `silver/occupancy_poi_hour` (2023–2025, 5km panel, 319 qualifying cells).

**Signal reliability** — split-half correlation of each cell's hour-of-week profile:

| cell size | cells ≥10 POIs | median POIs | split-half r | share r>0.9 |
|---|---|---|---|---|
| 250m | 319 | 25 | **0.990** | 84.3% |
| 500m | 142 | 55 | 0.993 | 88.7% |
| 1km | 50 | 130 | 0.997 | 94.0% |

Reliability is excellent at every resolution, so noise is *not* the binding constraint.

**Spatial correlation length** — correlation of residual log-share between cell pairs
after removing the citywide rhythm, by inter-cell distance:

| distance | daily r | **hourly r** | hourly, 1h lag |
|---|---|---|---|
| 250m | 0.134 | **0.445** | 0.440 |
| 500m | 0.069 | 0.367 | 0.362 |
| 750m | 0.029 | 0.257 | 0.249 |
| 1km | 0.012 | 0.154 | 0.151 |
| 1.5km | ~0.01 | 0.072 | 0.064 |

Three results follow:

1. **Correlation length is ~750m–1km**, so cells must be comfortably smaller for a
   graph to have structure to exploit. At 250m adjacent cells correlate 0.445 —
   informative but not redundant. At 1km they correlate 0.154 and the graph is
   effectively disconnected. **250m is near-optimal; 500m is the viable alternative;
   1km is unusable for a graph.** Heuristic: cell size ≈ ¼–⅓ of correlation length.
2. **Daily aggregation destroys the spatial signal** (0.134 vs 0.445). The
   redistribution is an hourly phenomenon; this is what justifies the 168-element
   explode.
3. **There is no lagged propagation.** Lag-1 correlation sits fractionally *below*
   contemporaneous at every distance. Activity redistributes simultaneously rather
   than travelling cell-to-cell like freeway congestion. This determines the Tier 2
   architecture (§7).

Caveat: measured inside the dense 5km panel. Outer SF has lower POI density, so the
citywide build must re-measure rather than assume these values transfer.

### Covariate coverage — the binding constraint on the hourly window

| table | span | rows |
|---|---|---|
| `weather_hour` | **2023-01-02 → 2025-12-31** | 26,142 |
| `event_hour` | 2023-01-02 → 2025-12-25 | 1,446 |
| `weather_day` | 2022-01-01 → 2025-12-31 | 1,461 |
| `calendar_day` | 2022-01-01 → 2025-12-31 | 1,461 |

Bronze gives hourly *presence* back to 2020, but the hourly *covariates* stop at
2023–2025. **The hourly model window is therefore 2023-01-02 → 2025-12-31 unless NOAA
LCD hourly is re-pulled** (free, already a registered source — listed as an expansion
in §11). Bronze's immediate win over silver is geography, not history.

### Events — 2023-2025 window, 1,095 days

| treatment | days |
|---|---|
| Giants home | 246 |
| **Chase Center** | **256** |
| Moscone | 77 |
| citywide | 18 |
| street fairs | 12 |
| flagged `clean_control` | 828 |

**Control-pool defect.** Of 849 non-game days, **268 carry another event**, leaving
only **581 genuinely clean days** — yet 828 are flagged `clean_control`. Roughly
**247 contaminated days sit in the control pool.** Gold QA records
`control_pool_sensitivity` at −0.3%, but that was measured at ring 1 (0–250m) where
Chase Center is distant and irrelevant. Chase sits in or adjacent to Oracle Park's own
neighbourhood, so at city grain the contamination is structurally different and must
be re-tested, not inherited. Fixing this is a §11 Phase-2 deliverable.

### Anchor — `bronze/cdtfa_taxable_sales/`

- `cdtfa_sf_by_business_type.csv` — SF × quarter × business type, **2015 → 2026 Q1,
  45 quarters**. `BusinessGroupCode='C08'` = Food Services and Drinking Places.
  2026 Q1: 5,385 permits, $1.299B taxable transactions.
- SF food POI panel is stable at **4,355–4,405** POIs (2023–2026), i.e. **~81% of the
  5,385 taxable food-service establishments the anchor counts.** This is the
  credibility number for the entire calibration; lead the report with it.

### Motivating result (already measured)

Near-field activity share, ex-venue balanced panel, train split: game days concentrate
**~35% more** of local activity within 500m of the ballpark than matched controls,
significant at every evening hour (t = 4.5 → 10.7), peaking at 21:00. Presented as
motivation only — real inference comes from the model's intervals, since these t-stats
are unadjusted for autocorrelation and multiple hours.

## 5. Architecture

Seven stages. Each lands Parquet and is independently inspectable.

1. **Registry.** Add SF Analysis Neighborhoods (DataSF, open) to `config/sources.yaml`
   with license and status *before* any ingester exists — CLAUDE.md rule.
2. **`ingest/advan_bronze.py`** — read `bronze/advan_weekly_patterns/*.parquet`
   filtered to SF, project only needed columns, parse both JSON arrays, explode to
   POI × date × hour (person-hours) and POI × date (visits). DuckDB against `s3://`;
   land partitioned by year. Run once.
3. **`transform/spatial_units.py`** — assign each POI a `unit_id`. **This is a lookup
   table and the entire spatial design is pluggable through it** (§6).
4. **`transform/panel.py`** — aggregate to unit × date × hour × business-group; join
   weather, calendar, events, and per-unit POI coverage counts.
5. **`transform/graph.py`** — build the edge set (§6).
6. **`nowcast/models/`** — the model tiers (§7).
7. **`calibrate/` + `nowcast/disaggregate.py`** — the dollars bridge (§8).

## 6. Spatial substrate and graph

**Default unit: 250m grid cells with ≥10 POIs → 452 nodes**, chosen on the measured
correlation length (§4), not by convenience. Cells aggregate up to the 41 Analysis
Neighborhoods for reporting, so outputs speak in names readers recognise while the
model runs at a resolution that supports a graph. **500m (281 nodes) is the pre-planned
variant** to test; 1km is excluded by measurement.

**Pluggability is the key de-risking property.** Stage 3 emits `poi_id → unit_id`.
Everything downstream is invariant to what a unit is. Neighbourhoods (41), census
tracts (~200–250), grid cells (452), or learned regions are swaps, not rewrites.

**Node features:** POI count; NAICS business-mix vector; food share; per-week coverage
count; distance and bearing to each venue; learned behavioural cluster type (§7).

**Edges**, three families, ablated separately so the report can say which mattered:

- **Contiguity** — queen adjacency between neighbouring cells.
- **Empirical distance decay** — k-NN weighted by the *measured* hourly correlation
  function (§4): 0.445 at 250m → 0.367 at 500m → 0.257 at 750m → 0.154 at 1km →
  0.072 at 1.5km, truncated where it reaches noise. Calibrated adjacency beats an
  arbitrary Gaussian or inverse-distance kernel, and costs nothing extra to build.
- **Flow similarity** — cosine over `VISITOR_HOME_CBGS`, the OD-like signal and the
  closest analogue to a PeMS OD matrix; connects functionally linked cells that
  distance-based edges miss entirely.

**Clustering enters as a feature, not as geometry.** Behavioural typology (office /
lunch / nightlife / residential-retail) is learned by clustering each cell's
normalised hour-of-week profile and attached as a node attribute. Defining the
*geometry* by clustering would make a fitted object the foundation of everything
downstream, owing k-justification, bootstrap stability, and a leakage guard, with the
whole spatial frame moving if the partition proves unstable. As a feature it carries
the same interpretive payload at none of that risk. Learned regionalisation
(contiguity-constrained Ward) remains available as a §11 upgrade.

**Leakage guard, non-negotiable:** any clustering is fit on **non-event days within
the training split only**. Otherwise clusters are partly defined by event response and
using them to measure event response is circular.

## 7. Models

**Panel:** 452 nodes × 26,280 hours = **11.9M node-hours**, dense. Splits reuse the
existing gold boundaries exactly, so results are comparable with the team's GNN work:
train 2023-01-02→2024-12-31, val 2025 H1, test 2025 H2.

**Target:** `log1p(person_hours)` per node-hour, quantile heads at 0.05 / 0.50 / 0.95.
Shares derived by renormalisation.

**Two model families**, sharing the whole feature pipeline:

- **Measurement model** — trained on non-event hours *only*, with no event features.
  The residual on event hours is causally readable precisely because the model has
  never seen an event. Feeds the dollar estimate.
- **Deployment model** — trained on all hours *with* event features (attendance,
  first pitch, relative hour, opponent). Forecasts a scheduled game's heat map. This
  is the MLOps artifact; its residuals are **not** causal.

**Tier 1 — pooled GBM.** LightGBM, unit as categorical, quantile objective. Fast,
strong, and the honest benchmark: at any node count a well-featured GBM is hard to
beat, and reporting that is more valuable than hiding it.

**Tier 2 — spatiotemporal graph network.** Node embedding (452 × d) + static features
→ temporal encoder (dilated 1-D conv or GRU over a 24–48h window) → 2–3 graph
convolution layers → per-node-hour head. **STGCN or Graph WaveNet style, not DCRNN**,
and this is now an empirical call rather than a stylistic one: §4 shows lag-1 spatial
correlation sitting fractionally *below* contemporaneous at every distance, so there
is no travelling wave for diffusion convolution to model. Activity redistributes
simultaneously. DCRNN's lagged-diffusion prior would be fitting structure the data
does not contain.

**Tier 3 — multi-event generalisation.** Chase Center's 256 event days are a second
venue in the same city with different geography. With one venue you cannot separate
"event effect" from "that neighbourhood's quirks"; with two you can. Held-out-venue
evaluation is the strongest generalisation claim available and satisfies D8's
multi-event-type requirement with no new ingestion.

**Baselines every tier must beat:** unit hour-of-week median; the v0 matched-control
mean; seasonal naive.

## 8. Dollars bridge

CDTFA anchors dollars in *time* (quarterly) but **not in space** — nothing sub-city is
published. A per-unit dollar map assuming uniform velocity is therefore unvalidatable,
and uniform velocity is certainly false.

**The defensible route:** `cdtfa_sf_by_business_type.csv` breaks dollars out by
business group, so calibrate **velocity per business type citywide** — which *is*
anchored — then apply per unit using that unit's own business mix. Velocity then
varies across space because business composition does, and every coefficient traces to
something CDTFA measured. Absent that, publish the heat map in presence units and
dollars only citywide.

Daily citywide dollars come from Chow-Lin (fallback Denton) distributing quarterly C08
across days using the citywide daily index, summing exactly to each observed quarter.

## 9. Hourly measurement drift — MEASURED, and worse than panel composition

Rev. 2 assumed the risk here was panel composition, mitigated by a fixed cohort.
**Measured 2026-07-31 on the landed citywide explode, that assumption is wrong.**

**The hourly array is not stationary.** Person-hours ÷ visits by quarter:

| quarter | ratio, all POIs | ratio, balanced cohort | non-zero hrs/POI |
|---|---|---|---|
| 2022 Q4 | 1.69 | — | 495 |
| 2023 Q1 | 2.34 ← documented vendor break | 2.83 | 346 |
| 2024 Q1 | 2.08 | 2.54 | 343 |
| 2025 Q4 | **1.69** | **2.09** | 256 |

A **−27% monotonic slide across the exact modelling window.** Every quarter-over-quarter
step is under 15%, so the team's `advan_hourly_construction_stable` gate passes each
one while the level drifts a quarter of its magnitude — the standard blind spot of a
QoQ-tolerance check.

**A fixed cohort does not fix it.** Restricting to POIs reporting in ≥95% of the 157
window weeks costs 77% of the panel (≈16,000 → 3,739 POIs) and still leaves a −26.2%
slide. The vendor is constructing the array differently for the *same* POIs; this is
not composition.

**It contaminates spatial shares.** Per-cell share of citywide activity, 2023 vs 2025:

| statistic | daily-derived | hourly-derived |
|---|---|---|
| corr(share 2023, share 2025) | **0.9967** | 0.9502 |
| median abs share change | **5.3%** | 29.0% |
| total variation | **2.9%** | 14.4% |
| sd of log change | **11.8%** | **61.7%** |

Correlation between hourly-derived and daily-derived log share-change is only **0.53**.
Real citywide redistribution is small; the hourly series shows ~5× more movement, half
of it unrelated to reality. Within-day *shape* holds up better but is not clean —
median cell profile correlation 0.89, only 27.4% of cells above 0.95, 11.4% median
shape total variation.

### Consequences for the design

1. **The event-study contrast is largely immune, and this is why the §4 motivating
   result stands.** Matched controls sit within ±120 days at the same weekday, and a
   slow monotonic drift over quarters barely moves a contrast that local. Game-vs-control
   is safe; 2023-vs-2025 is not.
2. **Model targets must be local deviations, not levels or global shares.** The Tier 1/2
   target becomes `log(activity) − log(local rolling baseline)` for that cell, baseline
   from a window of matched non-event days. Drift cancels inside the window. A raw-level
   target trained on 2023–24 and tested on 2025 H2 would be measuring the vendor.
3. **Daily visits carry the level; hourly carries only within-day shape.** Daily is the
   clean series (2.9% total variation) and remains the citywide dollars indicator.
   Hourly is used for allocation within a day, never for cross-year level or share.
4. **Ship a drift gate**, not a cohort filter: assert the person-hours ÷ visits ratio
   by quarter and fail loudly on cumulative (not just QoQ) movement.
5. **Panel composition still applies** as a secondary effect — POIs reporting hourly
   fell 9.5% over the window versus 2.2% for daily — but it is not the main problem.

**Open decision (see §11):** shortening the hourly window (e.g. 2023-07 → 2025-06)
reduces cumulative drift at the cost of games. Not yet decided.

## 10. Validation

| level | test |
|---|---|
| Reconciliation | daily dollars sum exactly to each observed quarter |
| Counterfactual | MAE / RMSLE on held-out non-event node-hours |
| Calibration | 90% interval coverage lands near 90% |
| Baselines | beats hour-of-week median, matched-control, seasonal naive |
| Spatial | error decomposed by unit; thin units surfaced not hidden |
| Ablation | contiguity vs flow vs distance edges, separately |
| Generalisation | held-out venue (train Giants → test Chase) |
| Placebo | event-detection procedure on random non-event days → ~0 |
| Congruence | panel food POIs vs CDTFA C08 permits (~81%) |
| Cross-check | citywide game-day uplift vs the ring-1 result in gold |

## 11. Build order (10 days: 2026-07-31 → 2026-08-10)

| phase | days | deliverable |
|---|---|---|
| 0 | **done** | **day-alignment gate PASSED** — see below |
| 1 | 1–2 | hourly explode landed; registry entries; POI→unit table; unit×hour panel |
| 2 | 3–4 | feature join; **control-pool fix**; splits; Tier 1 + validation |
| 3 | 5–7 | graph construction; Tier 2 STGNN; edge ablation vs Tier 1 |
| 4 | 7–8 | dollars bridge: Chow-Lin on C08 + business-mix velocity |
| 5 | 8–9 | Tier 3 held-out-venue; heat map artifacts; results |
| 6 | 10 | writeup, reproducibility, buffer |

### Measured results to date (2026-07-31)

**Phase 1.** 452 cells at 250m (>=10 POIs), 14,467/17,667 POIs retained (81.9%).
`cell_hour` 11,878,560 rows at **72.5% non-zero**, versus 15-21% at POI grain —
aggregation dissolves the sparsity that sank the gold GNN target. Oracle Park
itself falls below the POI floor and is excluded, which removes the tautology
risk rather than creating a gap. Validated end to end: the citywide path
reproduces the independent 5km/ring/balanced-POI result (+47.6% near-500m share
at 21:00 vs ~35% peaking at the same hour).

**Hourly construction drift — the finding that shaped everything after it.**
Person-hours per visit slides from 2.33 (2023 Q1) to 1.69 (2025 Q4), a **-27%
monotonic decline**. Every quarter-over-quarter step is under 15%, so the team's
`advan_hourly_construction_stable` gate passes each one while the level drifts
badly — the standard blind spot of a QoQ-tolerance check. Consequence: raw levels
are not comparable across the window, and effects must be differenced against
contemporaneous controls.

**Phase 2, Tier 1 (pooled GBM).** Held-out control hours, log1p target:

| features | val MAE | test MAE | test R2 | test bias |
|---|---|---|---|---|
| covariate-only | 1.1122 | 1.2282 | 0.627 | +0.2539 |
| **+ rolling baseline** | **0.9658** | **1.0307** | **0.7222** | **+0.0265** |

The bias collapse matters more than the accuracy gain: the trailing control-only
rolling baseline largely **solves** the drift rather than working around it,
because it is a local reference that moves with the panel. Tier 1 beats the
cell-hour-of-week baseline by 23.7%.

**Control pool.** Shipped vs strict differs by **-0.3%** test MAE at city grain —
the same figure gold QA measured at ring 1. The hypothesis that contamination
would bite harder citywide (Chase Center sits beside Oracle Park) is NOT
supported; strict costs 30% of training rows for no measurable gain.

**Effects, full window, 246 games vs 581 strict controls, day-clustered
bootstrap (2000 draws) + placebo (200 draws):**

| band | effect | 95% CI | p |
|---|---|---|---|
| 0-500m | **+43.4%** | [+37.9%, +49.4%] | <0.0001 |
| 500m-1km | **+14.1%** | [+11.3%, +16.9%] | <0.0001 |
| 1-2km | **+3.5%** | [+2.4%, +4.6%] | <0.0001 |
| 2-4km | +0.8% | [-0.1%, +1.6%] | 0.070 |
| >4km | +0.2% | [-0.8%, +1.1%] | 0.685 |

Near-ballpark activity rises sharply and precisely while the rest of the city does
**not** fall — so the near-field lift is net new activity, not spend relocated
from elsewhere in SF. That is the question section 2 adopted citywide geography to
answer. An earlier test-split-only estimate (39 games) showed -6.0% beyond 4km and
looked like displacement; on all 246 games it is null. Small-sample artifact,
recorded rather than dropped. Inference must cluster at the day: naive cell-hour
t-stats overstate by roughly 3x.

**Multi-depth baseline (supersedes single k=8).** k=8 spanned a median 112
calendar days on game days (max 203) because ~47% of same-weekday candidates are
event days. RMSE has an interior optimum near k=3-4 and combining depths beats any
single one. Refit on k2/k4/cap120:

| | single k=8 | multi-depth |
|---|---|---|
| val MAE | 0.9658 | **0.8655** |
| test MAE | 1.0307 | **0.9180** |
| test R2 | 0.7222 | **0.7569** |
| beats naive by | 23.7% | **32.0%** |

**Effects are robust to counterfactual quality.** Re-estimated on the 10.9%-better
model: +44.6 / +14.8 / +4.5 / +1.2 / +0.2 by band, versus +43.4 / +14.1 / +3.5 /
+0.8 / +0.2 before. The headline moved ~1pp. This is the DiD design doing its job,
and it bounds Tier 2's possible upside on the CAUSAL numbers at near zero even if
it eventually wins on MAE.

**Tier 2 status.** Run 1 (stride 24, 729 windows): every variant lost to Tier 1,
though the graph itself helped — distance -2.1% and flow -1.7% vs no-graph, while
raw contiguity HURT +1.6%, exactly as the 0.114 adjacent-cell correlation predicts.
Run 2 (stride 6) DIVERGED and is not evidence: neigh/self exploded to 2-4x against
the 1.197 init, best_epoch collapsed to 0-2, and even the no-graph config degraded.
Cause was changing stride, batch, epochs, dropout and stopping criterion at once,
leaving nothing attributable; epoch-granularity early stopping is far too coarse
when each epoch carries 4x the gradient steps.

**Tier 2 v3 — the fair comparison.** Step-level checkpointing fixed the schedule
failure; training is stable (neigh/self 0.98-1.00, no explosion). Identical
features, splits and masking to Tier 1:

| model | val | test | R2 | neigh/self | best step |
|---|---|---|---|---|---|
| **Tier 1 GBM** | **0.8655** | **0.9180** | **0.7569** | — | — |
| STGNN none | 1.0151 | 1.0897 | 0.7203 | 0.981 | 300/1100 |
| STGNN distance | 1.0108 | 1.0728 | 0.7241 | 0.996 | 300/1100 |
| STGNN flow | 1.0008 | **1.0689** | 0.7237 | 0.996 | 300/1100 |

Two findings. **The graph carries real signal** — flow beats no-graph by 1.9%,
distance by 1.6%, consistent in direction with run 1 and with the pre-registered
0.114 correlation (raw contiguity HURT; weighted and flow edges help). Which
weighted family wins is within noise; that both beat no-graph is not.
**The STGNN loses decisively to the GBM** — 1.0689 vs 0.9180, a 16% gap that is
no longer attributable to configuration.

This is the section 12.5 outcome: an STGNN that cannot beat Tier 1 is a
reportable finding. The likely reason is that `unit_code` is the GBM's dominant
feature by a wide margin, i.e. per-cell identity plus a strong temporal baseline
carries most of the signal, and 2,913 training windows is thin for a neural model
to recover that through a 32-dim embedding.

**Phase 3 expectations, recorded before fitting.** Citywide adjacent-cell hourly
residual correlation is only **0.114** at 250m — a quarter of the 0.445 measured
inside the dense 5km panel — and the gap is density, not distance (dense pairs
0.202, thin pairs 0.055). Tier 2's expected gain over Tier 1 is therefore MODEST,
and per section 12.5 an STGNN that cannot beat Tier 1 is a reportable finding.

**Phase 0 result (2026-07-31).** `ingest.advan_bronze.verify_alignment()` reproduces
`silver/occupancy_poi_hour` from bronze for week 2024-07-01: 351,207 rows matched,
**100% exact value equality, 0 rows on either side only**. Since silver was built
independently from the same bronze by the team pipeline, this confirms both the
Monday-start day alignment and the person-hours unit. All 334 `DATE_RANGE_START`
values are Mondays. The same explode yields 441,844 rows citywide for that week —
the 90,637-row surplus over silver is exactly the coverage beyond the 5km cap.

**Expansions, in priority order if time allows:** NOAA LCD hourly re-pull to extend the
window to 2020–2026; learned regionalisation as an alternate unit set; non-Giants event
magnitudes for dose-response; 2026 calendar extension.

## 12. Risks

1. **Day alignment — highest risk.** `DATE_RANGE_START` is the week start; an
   off-by-one in the 7- or 168-element explode silently shifts every event effect by a
   day and would look entirely plausible. Assert against a known weekly total *and* a
   known game date before anything downstream is trusted. Phase 0 gate.
2. **Control-pool contamination** (§4). Must be re-tested at city grain.
3. **Venue dominance.** The ballpark was 73.6% of near-field activity; whichever unit
   contains it will be swamped and "activity shifts toward the ballpark" becomes
   tautological. Compute every result with and without the venue POI.
4. **Panel composition** (§9).
5. **Tier 2 overfitting.** 11.9M node-hours is substantial, but 452 nodes over 3 years
   is a modest panel for a neural model. Early-stop on val; regularise; if the STGNN
   cannot beat Tier 1, report that — it is a finding, not a failure.
6. **Spatial dollars are unanchored** (§8). The business-mix route mitigates but does
   not eliminate this. Never claim validated sub-city dollar accuracy.
7. **CDTFA measures taxable transactions**, not all spend — groceries largely exempt,
   services out of scope. The claim stays "taxable food-service dollars."
8. **Presence ≠ people ≠ dollars.** Velocity is calibrated at the anchor grain and
   carried down with intervals, never assumed (CLAUDE.md).

## 13. Out of scope

- No changes to `silver/`, `gold/`, or the `eia-nowcast/` prefix — all build contracts.
- No multipliers; direct visitor spend only (D7).
- No claim of validated sub-quarterly or sub-city accuracy. Below quarter grain this is
  principled interpolation carrying quantified-but-unverifiable uncertainty (`docs/02`).
- No re-litigation of `docs/03` decisions; this design reverses none of them.
