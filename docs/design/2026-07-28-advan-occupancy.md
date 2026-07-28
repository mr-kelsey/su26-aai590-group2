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
0 or at least about 12.

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
table. Window is the existing `PANEL_START` to `PANEL_END`, 2022-01-01 to
2025-12-31, so 1,461 days.

### 6.1 `occupancy_ring_hour.parquet`

The analysis deliverable. Exactly 175,320 rows (1,461 days x 24 hours x 5 rings).

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

POI-grain detail, all 5 rings, sparse. Roughly 72M rows. Replaces the
hand-uploaded `silver/advan_hourly/` with reproducible build output.

| Column | Meaning |
|---|---|
| `footprint_id` | POI key |
| `date`, `hour` | local date and hour |
| `visitor_hours` | strictly greater than 0 |
| `ring_id` | ring assignment |
| `naics_code` | industry code |

Only non-zero hours are stored. 75.65% of POI-hours are zero, so sparsity cuts
the table from about 268M rows to about 72M.

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

1. report non-null `VISITS_BY_EACH_HOUR` in every Advan week in the window, and
2. were open for the whole window per `OPEN_DATE` and `CLOSE_DATE`, using the
   spec's sentinels (1970-01-01 means opened before 2010, 2038-01-01 means
   still open).

Condition 2 is an improvement over inferring presence from `weeks_present`
alone: a POI that closed mid-study should be excluded explicitly.

The size of this set must be measured during implementation. If ring 1 retains
fewer than 10 POIs, that gets reported and the criterion relaxed to coverage in
at least 95% of weeks rather than shipping a near-empty column silently.

## 7. Silver QA gates

Using the existing `gate()` helper. Failure aborts the build.

| Gate | Check |
|---|---|
| `advan_vbh_parse` | every non-null hourly array has exactly 168 elements |
| `advan_vbh_local_time` | on single-game days, ring-1 `visitor_hours` peak hour is within +/-3 of `first_pitch_hour` for at least 80% of games |
| `advan_scale_stability` | per week, the minimum non-zero `VISITS_BY_DAY` value stays inside [10, 16] and the median first-difference of the 20 smallest distinct non-zero values stays inside [10, 16] |
| `advan_vbh_dwell_crosscheck` | visitor-hours predicted from `BUCKETED_DWELL_TIMES` correlate with observed at 0.85 or better across POIs, with an aggregate ratio inside [0.7, 1.4] |
| `venue_hours_per_visit` | venue POI `visitor_hours / visits` inside [2.5, 6.0] on single-game days |
| `occupancy_panel_shape` | exactly 1,461 x 24 x 5 rows, zero duplicates |
| `occupancy_sparse_integrity` | no `visitor_hours <= 0` rows in the POI table, and every row's POI-week is marked covered |
| `occupancy_coverage_floor` | median `visit_share_covered` at least 0.80 in every ring |

`advan_vbh_local_time` is the highest-value gate. It is what caught the
semantics, and it fails loudly if anyone reintroduces a timezone conversion.

`advan_scale_stability` exists because Advan's roughly 12.3x scale-up is applied
by the vendor. If it drifts across 2022-2025, ring totals move for reasons
unrelated to games and every cross-season comparison silently breaks.

`advan_vbh_dwell_crosscheck` derives an independent prediction of visitor-hours
from the dwell distribution. For a visit of duration `d` minutes starting at a
random offset within an hour, expected buckets spanned is about `1 + d/60`.
Applying bucket midpoints (2.5, 12.5, 40, 150, 300 minutes) gives per-visit
multipliers of about 1.04, 1.21, 1.67, 3.50, 6.00. Predicted visitor-hours is
the dwell-weighted sum. Agreement with the observed hourly sum is strong
independent evidence for the bucket-spanning interpretation.

## 8. Gold table

Built by a new `build_occupancy_event_study()` in `pipeline/build_gold.py`.
Reads only silver.

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

## 9. Risks

**Build time.** The POI-hour table is about 72M rows against a current
one-minute silver build. Mitigation: measure on a single week and extrapolate
before running the full window, and partition the POI-hour output by year if it
does not stream cleanly through DuckDB.

**Thin balanced set.** Requiring hourly coverage in all roughly 209 weeks may
leave very few of ring 1's 37 POIs. Measured during implementation, with the
documented fallback in 6.4.

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
