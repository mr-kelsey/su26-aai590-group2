# How the citywide nowcast pipeline works

This is the usage guide to the citywide branch: I hightlights which scripts run, in what order, and
and any breaking dependencies.

## Running order

```
uv sync --extra model --extra dev
uv run pytest tests/
```

The tests need no credentials and no network, so they work on a fresh clone. The
pipeline itself does need S3 access, and it runs in this order:

1. `ingest/advan_bronze` pulls the raw Advan data and lands the POI dimension plus
   the daily and hourly fact tables.
2. `transform/spatial_units` cuts the city into 250m cells and decides which ones
   we keep.
3. `transform/panel` aggregates the facts up to cell grain.
4. `transform/features` joins on the covariates and builds the rolling baselines.
5. `transform/graph` builds the three edge sets the graph model uses.
6. `models/tier1_gbm` and `models/tier2_stgnn` are the two models.
7. `nowcast/effects` turns model residuals into the game-day effect estimates.
8. `nowcast/predict` composes the counterfactual and the effect into a game-night
   forecast, and scores it on held-out games.
9. `nowcast/tier3_venue` runs the Chase Center crossover check.
10. `nowcast/game_dollars` converts the effect into dollars.

Steps 1 to 5 only need to run once. Everything after that reads the landed Parquet. All output goes to `data/bronze_sf/`.

## Pulling the raw data

`ingest/advan_bronze.py` reads the raw Advan weekly patterns from bronze and
explodes the packed arrays into rows we can actually query. Across 334 weeks that
gives us 17,826 SF POIs, about 30.4M POI-days and 154.5M POI-hours. The whole thing
takes about 140 seconds.

In Advan, weeks start on Monday, so for a 1-based index i the day is `start + (i-1)//24` and
the hour is `(i-1)%24`. We verify this with `verify_alignment()`, which rebuilds
`silver/occupancy_poi_hour` from bronze and checks it matches exactly. It does:
351,207 rows, identical values, nothing extra on either side. Run that before
trusting anything downstream to ensure the data hasn't changed shape.

There are also two arrays with two different units.
`VISITS_BY_DAY` has 7 elements and counts visits. `VISITS_BY_EACH_HOUR` has 168 and
counts person-hours, since someone present for three hours is counted in all three.
They differ by roughly 4x and they do not reconcile.

## Cutting the city into cells

`transform/spatial_units.py` assigns every POI to a 250m grid cell and keeps the
cells holding at least 10 POIs. That leaves 452 cells covering 14,467 POIs, or 81.9%
of the city.

Everything downstream joins on `unit_id` and does not care what a unit actually is, so swapping
in 500m cells, census tracts or neighbourhood polygons means changing this file and
nothing else.

We picked 250m by measurement. Adjacent cells correlate at 0.445 on hourly residuals and the correlation length runs 750m to 1km, so a 250m cell sits at about a quarter of that. At that size neighbours still share real signal and they are not copies of each other. At 1km the correlation drops to
0.154 and a graph built on it has almost nothing to pass around. If anyone wants to
change the cell size, this measurement needs reconstructing first.


## Building the panel

`transform/panel.py` rolls the POI-level facts up to cells. The main output is
`cell_hour`, which is 11,878,560 rows (452 cells x 1,095 days x 24 hours) and 72.5%
non-zero. At POI grain the same data is only 15 to 21% non-zero, so a graph model does not become workable until we aggregate to cells.

We build against a full spine and zero-fill. Bronze only stores non-zero rows, so a missing cell-hour is a missing zero rather than a null. If you aggregate the sparse table directly you divide by the number of rows
that happen to exist rather than by all the hours, and every mean comes out inflated.
There is a test pinning this, and the error is roughly 32x.

We also build a weekly coverage table. The obvious drift covariate would be
`n_poi_reporting` at hour grain, but that column counts POIs with a non-zero hour, so
whenever the target is zero the covariate is zero too and it leaks. Counting distinct
POIs live in a cell across a whole week gives us the same information about panel
health without that drift problem.

## Features and baselines

`transform/features.py` produces `model_hour`, the single table both models read.
Covariates come from `gold/gnn_time_hour` rather than being rebuilt from silver,
which means our train/validation/test splits are identical to the team's GNN work
and the numbers are directly comparable.

We carry two definitions of a control day. The shipped `clean_control` flags 828
days but only excludes Giants games, while `clean_control_strict` also excludes
Chase, Moscone, citywide events and street fairs and leaves 581. The difference
between the two pools is -0.3% on test MAE, so either works. Both flags stay in the
table so the choice is a measured one.

We build three baselines at different depths rather than one. We let k signify the number 
of same day of the weeksto look back. We initially chose a single k=8 baseline, which
reached back a median of 112 calendar days on game days and as far as 203, because
roughly 47% of same-weekday candidates get skipped as event days. RMSE turns out to
have an optimum around k=3 to 4. We ran tests with individual k values and found that
combining depths beats any single one, prompting us to add multiple k values as features, 
with a cap of 120 calendar days.

## The graph edges

`transform/graph.py` builds three edge sets so we can ablate them separately:
contiguity (2,100 edges), distance (10,598) and flow (3,489).

Citywide, adjacent cells correlate at about 0.11, roughly a quarter of the 0.445
inside the dense 5km panel, and the gap tracks density rather than distance. Dense pairs correlate at 0.202 and thin outer pairs at 0.055. That bounds how much the graph can
add, and it makes a uniform distance kernel mis-specified. The distance edges are
therefore weighted by the measured correlation function and scaled by pair
density.

Flow edges are cosine similarity over `VISITOR_HOME_CBGS`, meaning cells that draw
visitors from the same places. That is the closest thing we have to an origin
destination matrix, and is the only family that can connect cells that are
functionally related but far apart.

## The models

Both models are trained on control hours with no event features at all, and neither one is a forecaster. The model has never seen a game and cannot have partly fitted one, so its residual on a game hour is causally readable. A model given event features would already have fitted the event, and the residual we measure the effect with would partly vanish into the fit.

Tier 1 is the benchmark. Test MAE 0.9180, R-squared 0.7569, beating a naive
cell-hour-of-week baseline by 32%.

Tier 2 is STGCN-style rather than DCRNN. Diffusion convolution models lagged
spatial propagation, and we measured lag-1 spatial correlation at or slightly below
contemporaneous at every distance, so there is no travelling wave to model here.
Best configuration is flow edges at test MAE 1.0689, which loses to the GBM by 16%.
The graph itself does help inside the STGNN family. Flow is 1.9% better than no graph and distance 1.6% better, while raw contiguity hurts by 1.6%, which is what a 0.11 correlation predicts.

## Turning residuals into effects

`nowcast/effects.py` estimates the game-day effect as a difference in differences on
residuals: the game-hour residual minus the contemporaneous control-hour residual.
Differencing against the same period cancels the vendor drift in the hourly data.

Inference clusters at the day. Cell-hours within a day are heavily
correlated, so naive cell-hour t-statistics overstate significance by roughly 3x. The
0-500m band reads t=7.2 unclustered but only z=+2.2 against a placebo.

Across 2023 and 2024, 163 games against 388 control days, with a 2,000-draw
day-clustered bootstrap:

| distance from the ballpark | effect | 95% CI | p |
|---|---|---|---|
| 0 to 500m | +37.3% | +32.4 to +42.2 | <0.001 |
| 500m to 1km | +14.3% | +11.1 to +17.5 | <0.001 |
| 1 to 2km | +3.0% | +1.7 to +4.4 | <0.001 |
| 2 to 4km | +1.1% | +0.1 to +2.1 | 0.030 |
| beyond 4km | +1.1% | -0.1 to +2.2 | 0.079 |

We estimate on those two years rather than the whole window, because the near-field
number is not stable across it:

| period | games | 0 to 500m effect | 95% CI |
|---|---|---|---|
| 2023 | 82 | +36.8% | +30.4 to +43.5 |
| 2024 | 81 | +37.7% | +31.1 to +44.8 |
| 2025 | 83 | +64.8% | +52.5 to +77.2 |
| all three | 246 | +46.1% | +40.9 to +51.4 |

The two earlier years agree closely and 2025 sits far above both. The reason is
measurement rather than economics. The vendor's device panel thins sharply through
2025, and as it thins, quiet control evenings drop below the threshold to register at
all before busy game evenings do, which widens the measured ratio without anything
changing on the ground. `docs/04_data_exploration.md` section 7 has the evidence,
including the balanced-panel split that separates a detection effect from a real one.
We take 2023 and 2024 as the credible estimate and read the full-window figure as inflated.

Fits are pinned deterministic, so rerunning reproduces these numbers exactly.

Nothing beyond 4km falls, so there is no sign of spending being pulled in from
elsewhere in San Francisco. That is evidence against displacement rather than proof of
new activity, since a null at that distance is also what you would see if displacement
were real but spread too thinly across the city to detect.

## Forecasting a game night

We did not build either model tier to forecast a game on its own. If you ask a counterfactual model about 8pm on a game night, it answers with the no-game number. `nowcast/predict.py`
composes the two things we do have into a forecast:

```
predicted game-night presence = counterfactual(date, no game) x (1 + band effect)
```

Every input exists before a game is played. The counterfactual needs calendar, weather and the trailing baseline. The effect comes from games already played.

Scored on game evenings within 500m, against the alternative of ignoring the game
entirely. Band effects come from train-split games only, so nothing about the held-out
games informs the prediction:

| split | game-evening cell-hours | ignore the game | composition | improvement |
|---|---|---|---|---|
| train, 2023 to 2024 | 6,520 | 0.6006 | 0.4974 | 17.2% |
| validation, 2025 H1 | 1,760 | 0.9691 | 0.8487 | 12.4% |
| test, 2025 H2 | 1,560 | 1.0454 | 0.8906 | 14.8% |

MAE on log1p presence. Train and test improve at comparable rates, which means the effect is not fitted to its own estimation window and it transfers to unseen games at roughly the same rate. Outside 500m the improvement falls to between
0.02% and 1.5%, which is what effects of 3.0%, 1.1% and nothing should produce.

The split is temporal, so 2023 and 2024 are training years and only 2025 is held out.
The tables in this module are keyed on split as well as year for that reason.

## Checking it against a second venue

`nowcast/tier3_venue.py` runs the crossover. With one venue you cannot tell an event
effect apart from a neighbourhood that simply behaves differently on summer
evenings. Chase Center gives us a second one: 1,188m away, with 211 event days that
do not overlap a Giants game. We drop the 45 days that do overlap rather than
controlling for them. Their inner rings share no cells at all.

| event days | measured around Oracle Park | measured around Chase Center |
|---|---|---|
| Giants only | +45.2% | -12.6% (not significant, p=0.11) |
| Chase only | +0.8% (not significant, p=0.78) | +650.0% |

Each venue's own ring responds to its own events and ignores the other's, 1.2km
away. A held-out time split could never have shown this, because a model that had
just memorised where the ballpark is would pass one.

This is a specification check rather than a held-out test. It pools every qualifying
event day, 201 Giants-only and 211 Chase-only, instead of fitting on some and scoring
on the rest. That does not weaken it much, because a neighbourhood story predicts all
four cells light up whichever days go in, so the argument rests on the pattern across the table. The counterfactual model is still trained on control hours only,
so no event day at either venue informs it.

The 650.0% is real rather than a glitch. Chase's inner ring is a single cell in newer
Mission Bay development where commerce is almost entirely event-driven, whereas
Oracle Park's surroundings have a busy independent SoMa economy running every night.
It rests on one cell, so the interval matters more than the point estimate.

## Getting to dollars

`nowcast/game_dollars.py` converts the effect into money using the existing
`nowcast/disaggregate.py`. Quarterly CDTFA food-service receipts are distributed
across days following the citywide food-visit indicator, and the result reconciles
to each quarter exactly (largest relative error 4.8e-15 across 12 quarters and
$14.18B).

That gives $67,819 per game, with a 95% range of $37.7k to $97.9k. Across the 163 home
games in 2023 and 2024 that is $11.1M, so roughly $5.5M in a season of about 82 home
games.

The dollars run over the same two years as the effects. Running them over the full
2023 to 2025 range instead moves the per-game figure by 2.6%, so matching the windows
costs almost nothing and saves having to explain which half of the pipeline the panel
thinning touches.

Three things in here hold the number down:

1. We apply the evening share of 34.5%, because the effects are estimated on hours
   16 to 23 and cannot be applied to a whole day.
2. The attributable fraction is e/(1+e) rather than e, because the observed activity
   already contains the effect.
3. The band beyond 4km enters as zero rather than as its noisy point estimate.

The next table shows where the money and the uncertainty actually come from:

| band | evening dollars per game | incremental |
|---|---|---|
| 0 to 500m | $62,563 | $16,996 |
| 500m to 1km | $72,117 | $9,022 |
| 1 to 2km | $808,382 | $23,545 |
| 2 to 4km | $1,677,842 | $18,255 |

The band we measure most confidently contributes the least. Most of the total comes from 1 to 4km, where a small effect with a proportionally wide interval multiplies a very large dollar base, so the overall range comes out wide. The tight near-field effect and the wide dollar figure do not contradict each other, because they measure different parts of the map.

One caveat is that velocity drifts. Dollars per visit rose 21% across the
anchor series and almost all of that is price rather than volume. Denton absorbs it
because it reconciles every quarter exactly, so the disaggregation still holds, but it
does mean velocity cannot be read as a structural parameter and nothing here can be
extrapolated past the anchor window.
