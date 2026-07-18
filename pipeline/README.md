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

Builds seven tables from bronze; the deliverable is `panel_ring_day.parquet`:
one row per (date, ring) with Advan visits (total / food-services / balanced
POI panel), Bay Wheels trips, the Giants-game treatment (attendance, day/night,
first pitch hour), confounder flags (ballpark, Chase, Moscone, citywide,
street-fair events), a clean-control flag, weather, and BART features.

Rings around Oracle Park (37.7786, -122.3893), edges in METERS:
`[0, 250, 500, 1000, 2500, 5000]` (metric standard adopted 2026-07-18; the
sources' only native distance unit is meters). Change `RING_EDGES_M` in one
place to re-ring everything. Window: 2022-01-01 to 2025-12-31 (full seasons
only; `PANEL_END`); 2020-2021 stays excluded by design.

QA gates fail the build loudly: Monday-aligned Advan weeks, VISITS_BY_DAY
parse + tolerance vs VISIT_COUNTS, panel shape (days x rings, no dups), full
visit coverage, and a reproduction of the known Aug-2024 Bay Wheels game-day
lift (~9.3%). A report-only cross-check compares our 0-300m visit series to
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

Builds the answer layer from silver (never bronze). Working today:
`game_effects` (per game x ring x measure: observed, matched-control
baseline, lift), `event_study_ring` (pooled effects with SEs; slices for
day/night and attendance terciles), and `distance_decay` (pct lift by ring
plus a log-linear fit in the manifest). Baseline v0 is a transparent
nearest-8 same-day-of-week clean-control mean (max 120 days out); a
regression baseline is the planned v1 and a natural agent-loop experiment.
Documented stubs: dollar calibration (blocked on multi-year CDTFA
disaggregation) and the impact-function model (SageMaker registry tie-in).

QA gates: full matched-control coverage, positive and significant core-ring
effect, outward decay, and a report-only comparison against Luke's
eia-nowcast residuals (corr 0.9965 on first build).

Promotion rule: build_gold.py writes locally and is NOT auto-published.
`gold/` in the bucket holds team-promoted builds only; agent-loop variants
go to `experiments/<run-id>/`.
