# Design: Advan hourly occupancy (visitor-hours) in silver and gold

Date: 2026-07-28
Author: Steve Farmer
Branch: `feature/advan-occupancy-hourly`
Status: proposed, pending team review of the semantics finding

## 1. Problem

`notebooks/advan_by_the_hour.ipynb` (merged in PR #2) reshapes Advan
`VISITS_BY_EACH_HOUR` into an hourly table and, for the POI-weeks where that
column is null, fills the gap by spreading the day's visit total across 24 hours
using a pooled shape.

That infill mixes two different quantities in one column. Advan's visit columns
dedupe unique visitors *within a bucket*, and the bucket size differs by column:

| Column | Dedup window |
|---|---|
| `VISITOR_COUNTS` | week |
| `VISIT_COUNTS`, `VISITS_BY_DAY` | day |
| `VISITS_BY_EACH_HOUR` | hour |

This is Advan's own definition. Per the v2.8 spec, `VISIT_COUNTS` is "the sum of
each day's unique visitors across the days in the range." Applying the same
convention one level down, `VISITS_BY_EACH_HOUR` is each hour's unique visitors.
A visitor present across four hourly buckets counts in all four.

So the hourly column is **visitor-hours**, not a headcount split across the day.
Summing it over a day yields roughly (visitors x hours on site), which exceeds
`VISITS_BY_DAY` by a dwell-dependent factor. The notebook's real rows carry
visitor-hours while its infilled rows carry visits spread thin, in one column,
with no flag. Coverage is also not random: hourly is null for 74.9% of POIs in
the 10-to-99-visit tier versus 11.0% of the 1000+ tier, so the synthetic rows
concentrate in small POIs.

This design takes the hourly column for what it is, drops the infill, and
publishes it at the grain the study needs.

## 2. Evidence

Measured on `S3/advan_weekly_patterns/` week of 2024-08-05 (17,097 POI-weeks).

**Hourly-to-daily ratio tracks dwell**, which is what bucket-spanning predicts:

| Median dwell | hourly / daily |
|---|---|
| under 30 min | 0.86 |
| 30 to 59 min | 1.12 |
| 1 to 2 hr | 1.34 |
| 2 to 4 hr | 2.07 |
| 4 hr and up | 3.17 |

`corr(ratio, dwell_hours) = 0.48`. Aggregate across the week: 2.09x.

**Hour buckets are POI-local time.** Oracle Park, same week:

| Date | Schedule `first_pitch_hour` | Occupancy peak hour |
|---|---|---|
| Aug 9 | 19 (night) | 19 |
| Aug 10 | 13 (day) | 15 (mid-game) |
| Aug 11 | 13 (day) | 13 |

Aug 5 to 8 are non-game days with day totals of 2,092 to 4,651 against 59,893
to 110,898 on the three game days. A UTC reading would place the Sunday matinee
at 06:00. The v2.8 spec confirms local time for both `DATE_RANGE_START` and the
hourly array.

**Per-day ratios at the venue are stable on game days**: 3.92, 4.21, 3.90.
Roughly 2.8 hours on site producing about 4 whole-hour buckets is what
bucket counting predicts.

**The Aug 9 curve for a 19:00 first pitch:**
`16h=6781, 17h=9779, 18h=13906, 19h=15037, 20h=11204, 21h=9238, 22h=2654`.

**Coverage is far better when visit-weighted than POI-weighted:**

| Ring | POIs | % POIs covered | % ring visits covered |
|---|---|---|---|
| 0-250m | 37 | 56.8 | 94.7 |
| 250-500m | 124 | 67.7 | 94.0 |
| 500m-1km | 293 | 69.3 | 92.9 |
| 1-2.5km | 4912 | 74.8 | 96.3 |
| 2.5-5km | 6019 | 60.6 | 86.8 |

Food services (NAICS 722) in the core ring is the weak spot: 8 of 16 POIs and
67.2% of food visits.

**Values are quantized.** Distinct small `VISITS_BY_DAY` values are 12, 25, 37,
49, 62, 74, 86, 98, 110, 123, which is Advan's scale-up from raw panel devices
at roughly 12.3 estimated visits per observed device. Hourly is on the same
scale (most common non-zero value is 13). A single POI-hour is therefore either
0 or at least about 12. The scale drifts gently with panel growth (yearly
median min-nonzero daily value: 18, 14, 12, 12 for 2022-2025; max week-over-week
step 5), which is smooth drift, not a break.

### 2.1 Vendor construction break at 2023 Q1 (found during implementation)

The hourly array itself has a discontinuity the daily array does not. On a
fixed set of 4,284 POIs reporting hourly in all four years (composition held
constant), quarterly `sum(hourly) / sum(daily)`:

| Quarter | Ratio |
|---|---|
| 2022 Q1-Q4 | 1.647, 1.638, 1.648, 1.638 |
| 2023 Q1 | 2.772 |
| 2023 Q2-Q4 | 2.652, 2.504, 2.495 |
| 2024 | 2.504, 2.534, 2.330, 2.304 |
| 2025 | 2.187, 2.130, 2.065, 2.066 |

A +69% step at the 2022/2023 boundary on constant composition means Advan
changed how `VISITS_BY_EACH_HOUR` is constructed. Corroborating: the minimum
non-zero hourly value is 18 in 2022 and 1 from 2023 on, so the value grid
changed too. Within 2023-2025 the ratio drifts smoothly (max quarter-over-
quarter change about 6%).

**Decision (Steve, 2026-07-28): the occupancy window is 2023-01-02 to
2025-12-31.** 2022 visitor-hours are not comparable to 2023+ and are excluded.
The window starts at the first Monday-aligned Advan week of the new era
because the week of 2022-12-26 straddles the break and would leak one
old-construction day (2023-01-01, an offseason Sunday) into the panel. The
daily visits panel is unaffected and keeps its full 2022-2025 window; every
existing estimate stands. Three seasons, roughly 240 home games, remain.

## 3. Non-goals

- No change to `visits_ring_day` or any existing silver table.
- No change to `game_effects`, `event_study_ring`, or `distance_decay`.
- No re-estimation of any current result. This work is purely additive.
- No imputation of missing hourly data, anywhere, for any reason.
- No dollar calibration of visitor-hours. That is downstream and out of scope.

## 4. Naming

The measure is `visitor_hours` throughout. One unit is one estimated visitor
present during one hourly bucket. It is not a headcount and not a visit count.

`visitor_hours` is preferred over `presence_hours` because it maps to Advan's
own vocabulary and reads as a standard compound unit. Every table README states
the definition and the arithmetic that demonstrates it: `visitor_hours` divided
by `visits` is average whole-hour buckets spanned, which lands near 4 for
ballgames.

## 5. Time semantics

Hour index `i` of `VISITS_BY_EACH_HOUR` maps to local date
`DATE_RANGE_START + floor(i / 24)` at local hour `i % 24`.

The parquet stores `DATE_RANGE_START` as `timestamp[ms, tz=UTC]` with a naive
midnight value. That UTC label is an artifact of the Dewey delivery, not
Advan's intent. The spec says local Monday midnight with a local GMT offset.
The code ignores the label deliberately, with a comment saying why, and gate
`advan_vbh_local_time` defends the choice.

## 6. Silver tables

Built by a new `build_occupancy()` in `pipeline/build_silver.py`, mirroring
`build_visits_ring_day()`. Reads bronze Advan and the existing `poi_rings`
table. Window is `OCC_START` to `OCC_END`, **2023-01-02 to 2025-12-31**
(1,095 days: 364 + 366 + 365), narrower than the daily panel per section 2.1.
Only weeks fully inside the window are read; a straddling week carries the
pre-break construction.

### 6.1 `occupancy_ring_hour.parquet`

The analysis deliverable. Exactly 131,400 rows (1,095 days x 24 hours x 5 rings).

| Column | Meaning |
|---|---|
| `ring_id`, `ring` | ring index and label, from `poi_rings` |
| `date` | local date |
| `hour` | local hour, 0 to 23 |
| `visitor_hours` | sum over covered POIs in the ring |
| `visitor_hours_ex_venue` | venue POI excluded |
| `visitor_hours_food` | NAICS 722 subset |
| `visitor_hours_balanced` | balanced-panel POIs only, see 6.4 |
| `poi_covered` | POIs in the ring with a non-null hourly array for the Advan week containing `date` |
| `poi_total` | POIs assigned to the ring |
| `visit_share_covered` | share of the ring's visits on that `date` sitting on covered POIs |

Rows exist for every ring-hour in the window even when `visitor_hours` is 0, so
the panel is dense and shape-checkable.

Note that coverage is a POI-week property, so `poi_covered` and
`visit_share_covered` do not vary across the 24 hours of a date. They are
repeated on each hourly row for convenience. A POI can be covered for the week
and still contribute 0 in a given hour, which is a true zero rather than a gap.

### 6.2 `occupancy_poi_hour.parquet`

POI-grain detail, all 5 rings, sparse. Measured: 49,612,710 rows on the 2023+
window. Replaces the hand-uploaded `silver/advan_hourly/` with reproducible
build output.

| Column | Meaning |
|---|---|
| `footprint_id` | POI key |
| `date`, `hour` | local date and hour |
| `visitor_hours` | strictly greater than 0 |
| `ring_id` | ring assignment |
| `naics_code` | industry code |

Only non-zero hours are stored. About 75% of POI-hours are zero, so sparsity
cuts the table roughly fourfold.

### 6.3 `occupancy_poi_week_coverage.parquet`

Required to read the sparse table without ambiguity. Roughly 2.6M rows
(covered POIs x about 209 weeks).

| Column | Meaning |
|---|---|
| `footprint_id` | POI key |
| `week_start` | Advan week, local Monday |
| `has_hourly` | whether `VISITS_BY_EACH_HOUR` was non-null |

Reading rule, stated in the README: a missing row in `occupancy_poi_hour` means
a true zero when `has_hourly` is true for that POI-week, and unknown when it is
false. Without this table, absence cannot be distinguished from non-reporting.

### 6.4 Balanced panel definition

`visitor_hours_balanced` restricts to POIs that both:

1. report non-null `VISITS_BY_EACH_HOUR` in at least 95% of the window's Advan
   weeks (ceil(0.95 x 157) = 150 of 157), and
2. were open for the whole window per `OPEN_DATE` and `CLOSE_DATE`, using the
   spec's sentinels (1970-01-01 means opened before 2010, 2038-01-01 means
   still open).

Condition 2 is an improvement over inferring presence from `weeks_present`
alone: a POI that closed mid-study should be excluded explicitly.

Measured outcome (2026-07-28): the strict all-157-weeks criterion kept only 5
core-ring POIs, so the documented fallback shipped. With both conditions (95%
coverage + open all window) the set is 3,148 POIs, 6 in ring 1. The ring-1 thinness is structural
(only about 21 of its 37 POIs report hourly in any week), so
**`visitor_hours_balanced` is a weak column in the core ring under any
criterion** and the READMEs say so. Rings 2-4, where the GNN nodes live, get
the real benefit of the relaxation.

## 7. Silver QA gates

Using the existing `gate()` helper. Failure aborts the build.

| Gate | Check |
|---|---|
| `advan_vbh_parse` | every non-null hourly array has exactly 168 elements |
| `advan_vbh_local_time` | on single-game days, ring-1 `visitor_hours` peak hour is within +/-3 of `first_pitch_hour` for at least 80% of games |
| `advan_hourly_construction_stable` | on POIs with hourly in every window year, quarterly `sum(hourly)/sum(daily)` changes at most 15% quarter over quarter; plus a hard assert that `OCC_START >= 2023-01-02` so the 2022 break stays excluded |
| `advan_vbh_dwell_crosscheck` | visitor-hours predicted from `BUCKETED_DWELL_TIMES` correlate with observed at 0.85 or better across POIs, with an aggregate ratio inside [0.7, 1.4] |
| `venue_hours_per_visit` | venue POI `visitor_hours / visits` inside [2.5, 6.0] on single-game days |
| `occupancy_panel_shape` | exactly 1,461 x 24 x 5 rows, zero duplicates |
| `occupancy_sparse_integrity` | no `visitor_hours <= 0` rows in the POI table, and every row's POI-week is marked covered |
| `occupancy_coverage_floor` | median `visit_share_covered` at least 0.80 in every ring |

`advan_vbh_local_time` is the highest-value gate. It is what caught the
semantics, and it fails loudly if anyone reintroduces a timezone conversion.

`advan_hourly_construction_stable` exists because the vendor has already
changed the hourly construction once (section 2.1, +69% at 2023 Q1). The gate
holds the window on the far side of that break and fails the build if a future
re-break lands inside it, instead of letting every cross-season comparison
silently shift. The fixed-POI set makes composition change unable to fake
stability.

`advan_vbh_dwell_crosscheck` derives an independent prediction of visitor-hours
from the dwell distribution. For a visit of duration `d` minutes starting at a
random offset within an hour, expected buckets spanned is about `1 + d/60`.
Applying bucket midpoints (2.5, 12.5, 40, 150, 300 minutes) gives per-visit
multipliers of about 1.04, 1.21, 1.67, 3.50, 6.00. Predicted visitor-hours is
the dwell-weighted sum. Agreement with the observed hourly sum is strong
independent evidence for the bucket-spanning interpretation.

## 8. Gold: hourly event study

Built by a new `build_occupancy_event_study()` in `pipeline/build_gold.py`.
Reads only silver. Covers the occupancy window (2023+), so it pools three
seasons of a single vendor construction.

### `occupancy_event_study.parquet`

| Column | Meaning |
|---|---|
| `ring_id`, `ring` | ring |
| `measure` | one of `visitor_hours`, `visitor_hours_ex_venue`, `visitor_hours_food`, `visitor_hours_balanced` |
| `relative_hour` | hours from first pitch, -6 to +8 |
| `slice` | `all`, `day`, `night` |
| `baseline` | matched-control mean |
| `effect` | observed minus baseline |
| `se`, `t` | pooled standard error and t statistic |
| `n_games` | games contributing to the cell |

Baseline reuses the existing estimator unchanged in spirit and in constants:
`MATCH_K = 8` nearest same-day-of-week clean-control days within
`MATCH_MAX_WINDOW_DAYS = 120`, cells with fewer than `MIN_CONTROLS = 5` matches
get a NULL baseline. The one addition is that matching is also on hour-of-day,
so the counterfactual for 19:00 on a game Friday is 19:00 on the 8 nearest
clean Fridays.

Two correctness requirements, both easy to get wrong:

**Cross-midnight.** `relative_hour` is computed on timestamps, not on
`(date, hour)` pairs. A 19:00 first pitch plus 8 hours is 03:00 the following
calendar day. Integer hour subtraction inside a single date silently yields
`relative_hour = -16` attached to the wrong day.

**Doubleheaders.** Games with `n_games > 1` are excluded, because two first
pitches make `relative_hour` undefined. The existing daily code uses
`MIN(first_pitch_hour)`, which is harmless at day grain and wrong at hour grain.
The excluded game count is recorded in the build manifest.

### Gold QA gates

| Gate | Check |
|---|---|
| `occupancy_es_coverage` | no ring x measure x relative_hour cell with fewer than `MIN_CONTROLS` matched controls |
| `occupancy_es_peak_at_zero` | for ring 1, the largest positive effect falls at `relative_hour` between -1 and +2 |
| `occupancy_es_outer_null` | ring 5 pooled effect is not significantly positive, preserving its role as a placebo ring |

## 8b. Gold: GNN data contract

The impact model is a GNN (team decision, 2026-07-28), framed as two parts:

1. **Counterfactual forecaster.** Train on clean non-game node-hours to
   predict `visitor_hours` per POI-hour from graph structure + calendar +
   weather. Lift = observed minus prediction on game hours. This is exactly
   the "regression baseline v1" the gold scaffold already anticipates, so the
   matched-control v0 stays as the benchmark it must beat. Effective sample is
   millions of node-hours, not ~240 games.
2. **Generalization head.** Maps game covariates (attendance, first pitch,
   day/night, dow, month) to predicted per-ring lift, for the "project any
   future event" deliverable. Labels come from the v0 estimator.

Node scope: rings 1-4 (inside 2.5 km), 5,842 POIs, hourly. Model training
itself is downstream of gold (SageMaker, registered per the existing promotion
gate); gold's job is the reproducible data contract, built by
`build_gnn_tables()` in `pipeline/build_gold.py`:

| Table | Grain | Content |
|---|---|---|
| `gnn_nodes.parquet` | 5,842 POIs | footprint_id, ring, dist_m, lat/lon, NAICS, top_category, balanced + venue flags, weeks covered |
| `gnn_edges_spatial.parquet` | ~5,842 x 8 x 2 | k-nearest by haversine (K_SPATIAL = 8), symmetric, no self-loops, dist_m weight |
| `gnn_edges_catchment.parquet` | top-k per node | cosine similarity of window-aggregated `VISITOR_HOME_CBGS` vectors (K_CATCH = 8, MIN_COS = 0.1), symmetric |
| `gnn_time_hour.parquet` | 26,280 hours | hour spine x calendar: game flags and covariates, clean_control, confounder flags, weather, relative_hour (NULL off game days), split |
| `gnn_target_node_hour.parquet` | sparse | footprint_id, date, hour, visitor_hours (from silver, rings 1-4) |
| `gnn_node_week_coverage.parquet` | node x week | has_hourly mask; missing target + covered week = true zero, uncovered = unknown |
| `gnn_game_labels.parquet` | game x ring x measure | v0 lifts from `game_effects` (2023+ games) for the generalization head |

Splits are temporal and parameterized: train 2023-2024, validation 2025 H1,
test 2025 H2, stamped on `gnn_time_hour.split`. The catchment edges are the
one place gold reads bronze (the CBG vectors are not in any silver table);
`build_gold.py` takes `--bronze-advan` and says so in its docstring.

`VISITOR_HOME_CBGS` caveats carried into the README: 66% fill, privacy-floored
(groups under 2 devices dropped, 2-4 reported as 4), and Advan's own guidance
is to treat the trade-area columns as ratios. Cosine similarity is exactly the
ratio-style use, and nodes without vectors simply get no catchment edges
(spatial edges still connect them).

Gold gates for the contract: node count matches rings 1-4 in `poi_rings`;
edges symmetric with no self-loops; at least half the nodes carry a catchment
edge; target row count equals the filtered silver count; splits partition the
window with no overlap; every clean-control training hour is truly non-game.

## 8c. Covariates: hourly weather and competing-event windows (added 2026-07-28)

The GNN forecaster conditions on covariates instead of shrinking the control
pool, so the covariates must be present and accurate at the model's grain.
Steve approved two data-ingest-gate items on 2026-07-28: NOAA LCD hourly
weather, and an ESPN re-pull carrying event start times.

**Findings made during this work:**

1. **The shipped NBA/WNBA dates carried a UTC +1-day bug.** ESPN event
   datetimes are UTC (a 19:00 PT tip is 02:00Z the next day) and the original
   ingest truncated the string to a date: 904 of 992 NBA rows and 63 of 93
   WNBA rows were dated one day late. This is the MLB `gameDate` lesson
   repeated on a second feed. Fixed in the re-pull (`date` is now the local
   game date; `start_utc` and `start_hour_local` added; `utc_date_was` keeps
   the old value for audit). Every `chase_event` day flag moves to the correct
   date on rebuild. The estimator is unaffected (`clean_control` never used
   chase), but the chase covariate is now right.
2. **LCD v2 access files are the METRIC edition** (deg C / mm / m/s) while
   the GHCN daily files are standard (deg F / inches / mph). The
   `weather_hour_vs_daily` gate caught this (p95 diff 51.6 on first build);
   silver converts hourly to F / inches / mph so the layer has one convention.
3. **Downtown SF (USW00023272) has no hourly observations at all** (its LCD
   file is daily summaries), so hourly weather is SFO-only, about 12 miles
   from the park. Fine for covariate use; the daily panel keeps downtown-first
   tmax.

**New silver tables (occupancy window):**

- `weather_hour`: date x hour temp/precip/wind (F / inches / mph), LST
  converted to America/Los_Angeles wall clock. Gates: coverage >= 95% of
  window hours (measured 99.5%); same-station daily-max cross-check
  (p95 = 2.0 F).
- `event_hour`: SPARSE Chase Center windows. NBA/WNBA home games use the real
  tip-off: hours tip-2 through tip+3. Concerts have no times in setlist.fm and
  use a documented default 19:00-23:00. Moscone/citywide stay day-grain.
  Gate: every flagged date is a chase_event day.
- `calendar_day.us_federal_holiday`: actual holiday dates (not observed-day
  shifts; traffic responds to the day itself), static in-code list 2022-2026.

**Gold changes:**

- `gnn_time_hour` gains: tmin/tavg/awnd (were already in the panel),
  `us_federal_holiday`, hourly `temp_hr`/`prcp_hr`/`wind_hr`, and
  `chase_event_hour`. New gates: `gnn_weather_coverage` (>= 95% of spine
  hours), `gnn_event_hour_within_day` (hourly flag implies the day flag).
- `occupancy_event_study` estimator unchanged. A REPORT-ONLY
  `control_pool_sensitivity` entry recomputes the ring-1 peak with a strict
  pool (chase/moscone days excluded from controls) and records the percent
  change; a large delta is a team conversation, not a build failure.

## 9. Risks

**Build time.** Measured 2026-07-28: `build_occupancy()` takes about 8 minutes
(49.6M sparse rows), on top of the roughly 1-minute daily build. Under the
15-minute bar set for partitioning, so the output stays unpartitioned.

**Thin balanced set.** Confirmed and structural in the core ring: 5 POIs
strict, 6 relaxed (section 6.4). The shipped criterion is the documented 95%
fallback; ring-1 `visitor_hours_balanced` is flagged weak in every README.

**Food coverage in the core ring.** `visitor_hours_food` for ring 1 rests on 8
of 16 POIs and 67.2% of food visits. It is the weakest number in this design
and the README says so explicitly.

**Quantization at hourly grain.** A single POI-hour is 0 or at least about 12,
so per-POI hourly precision is coarse. Ring aggregates average this out, but
the sparse table README must warn against reading individual POI-hours as
precise.

**Semantics not yet team-ratified.** The bucket-dedup reading is confirmed
against the v2.8 spec and four independent empirical checks, but the team has
not signed off. Everything here lives on a disposable branch until they do.

## 10. Rollback

All work is on `feature/advan-occupancy-hourly`. `main` is protected and
requires a reviewer, so there is no accidental-merge path.

Nothing is published to S3 as part of this work. Builds go to a scratch output
directory, never to `capstone/silver` or `capstone/gold`, so the currently
published and validated layers stay untouched.

`silver/advan_hourly/` in the bucket is left in place for now. Once
`occupancy_poi_hour` proves out, that folder can be removed, and bucket
versioning is enabled with no lifecycle rule so the removal is reversible.

Discard everything:

```bash
cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2 && git checkout main && git branch -D feature/advan-occupancy-hourly
```
