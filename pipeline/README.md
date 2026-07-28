# pipeline/

Code that builds the project's data layers. Medallion convention in the team
bucket `s3://aai-590-group2-capstone` (us-east-2):

| Layer | Prefix | What | Built by |
|---|---|---|---|
| bronze | `bronze/` | raw source pulls, byte-true (Advan, MLB, NOAA, BART, Bay Wheels, CDTFA, OI, confounder calendars) | ingest scripts staged with the data (`bronze/ingest.py` etc.); hand-gathered where APIs are blocked |
| silver | `silver/` | conformed + joined analysis tables, ring x day grain | `build_silver.py` (this folder) |
| gold | `gold/` (future) | model-ready / reported outputs | downstream modeling code |

Rule of thumb: bronze is precious (never `--delete`), silver and gold are
disposable (always rebuilt from code, synced with `--delete`).

## build_silver.py

Builds ten tables from bronze. The daily deliverable is `panel_ring_day.parquet`:
one row per (date, ring) with Advan visits (total / food-services / balanced
POI panel), Bay Wheels trips, the Giants-game treatment (attendance, day/night,
first pitch hour), confounder flags (ballpark, Chase, Moscone, citywide,
street-fair events), a clean-control flag, weather, and BART features.

Rings around Oracle Park (37.7786, -122.3893), edges in METERS:
`[0, 250, 500, 1000, 2500, 5000]` (metric standard adopted 2026-07-18; the
sources' only native distance unit is meters). Change `RING_EDGES_M` in one
place to re-ring everything. Window: 2022-01-01 to 2025-12-31 (full seasons
only; `PANEL_END`); 2020-2021 stays excluded by design.

Occupancy (visitor-hours) tables, added 2026-07-28:

- `occupancy_ring_hour.parquet` (ring x date x hour, dense; the hourly
  deliverable), `occupancy_poi_hour.parquet` (POI x date x hour, sparse
  non-zero), `occupancy_poi_week_coverage.parquet` (which POI-weeks report
  hourly; disambiguates true zero from unknown).
- `visitor_hours` is Advan `VISITS_BY_EACH_HOUR` taken for what it is:
  presence per hourly bucket (a fan at a 3-hour game spans ~4 buckets), NOT a
  headcount. Never add it to visits. Nulls (~38% of POI-weeks) are NEVER
  imputed; coverage columns are published instead.
- Occupancy window is `OCC_START..OCC_END` = 2023-01-02 to 2025-12-31,
  NARROWER than the daily panel: Advan changed the hourly construction at
  2023 Q1 (+69% on a fixed POI set), so 2022 visitor-hours are excluded. The
  daily tables are unaffected.

Hour-grain covariate tables (added 2026-07-28, same occupancy window):

- `weather_hour.parquet` - NOAA LCD hourly temp/precip/wind, SFO only
  (downtown has zero hourly obs). LCD ships METRIC; converted to F/inches/mph
  to match weather_day. LST converted to wall clock.
- `event_hour.parquet` - sparse Chase Center event windows from real NBA/WNBA
  tip-offs (the 2026-07-28 ESPN re-pull also fixed a UTC +1-day date bug on
  904 NBA + 63 WNBA rows, so chase_event day flags moved to their correct
  dates); concerts use a documented 19:00-23:00 default.
- `calendar_day.us_federal_holiday` - static in-code list, actual dates.

QA gates fail the build loudly: Monday-aligned Advan weeks, VISITS_BY_DAY
parse + tolerance vs VISIT_COUNTS, panel shape (days x rings, no dups), full
visit coverage, a reproduction of the known Aug-2024 Bay Wheels game-day
lift (~9.3%), and for occupancy: 168-element parse, the construction-break
tripwire (fixed-POI quarterly ratio drift <= 15%), local-time alignment of
ring-1 peaks to first pitch, venue hours-per-visit plausibility, a dwell-
distribution cross-check, sparse-table integrity, panel shape, and a
coverage floor. A report-only cross-check compares our 0-300m visit series to
Luke's `eia-nowcast` residuals input.

Run (see the module docstring for paste-ready commands):

```
python pipeline/build_silver.py --bronze <bronze path or s3://...> --out <dir>
aws s3 sync <dir> s3://aai-590-group2-capstone/silver --delete --region us-east-2
```

Dependencies: `pip install -r pipeline/requirements.txt` (DuckDB only).
Notes: the Bay Wheels step needs a local bronze copy (zips must be unzipped);
with an s3 bronze it is skipped and bike columns come out NULL.

Every build writes `build_manifest.json` (params, row counts, QA results,
bronze snapshot, git SHA) and a `README.md` into the output, so the silver
prefix always documents exactly which build it holds.

## build_gold.py (scaffold)

Builds the answer layer from silver. Working today: `game_effects` (per game
x ring x measure: observed, matched-control baseline, lift),
`event_study_ring` (pooled effects with SEs; slices for day/night and
attendance terciles), `distance_decay` (pct lift by ring plus a log-linear
fit in the manifest), `occupancy_event_study` (visitor-hours by hour relative
to first pitch, -6..+8, matched on day-of-week AND clock hour; doubleheaders
excluded; 2023+ occupancy window), and the `gnn_*` data contract for the
impact model. Baseline v0 is a transparent nearest-8 same-day-of-week
clean-control mean (max 120 days out); the GNN counterfactual forecaster is
the planned v1 and must beat it. Documented stubs: dollar calibration
(blocked on multi-year CDTFA disaggregation) and the impact-function model
itself (training runs downstream on SageMaker; registry tie-in unchanged).

GNN contract (design doc sections 8b-8c): `gnn_nodes` (rings 1-4, 2.5 km),
`gnn_edges_spatial` (8-NN haversine), `gnn_edges_catchment`
(VISITOR_HOME_CBGS cosine; the ONE bronze input gold takes, via
`--bronze-advan`), `gnn_time_hour` (hourly covariate spine: game treatment,
day + HOURLY weather, holiday, day + HOURLY Chase event flags, train/val/test
splits), `gnn_target_node_hour` (sparse visitor-hours),
`gnn_node_week_coverage` (true-zero vs unknown mask), `gnn_game_labels`
(v0 lifts for the generalization head). The occupancy event study also
records a report-only `control_pool_sensitivity` (strict vs default control
pool) in the manifest.

QA gates: full matched-control coverage, positive and significant core-ring
effect, outward decay, occupancy-event-study coverage / peak-near-pitch /
outer-ring-null, and the gnn_* structural gates (node count, edge symmetry,
catchment coverage, target-silver match, split partition, no leakage). A
report-only comparison against Luke's eia-nowcast residuals (corr 0.9965 on
first build) is unchanged.

Promotion rule: build_gold.py writes locally and is NOT auto-published.
`gold/` in the bucket holds team-promoted builds only; agent-loop variants
go to `experiments/<run-id>/`.
