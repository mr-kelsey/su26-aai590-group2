# 02 — Modeling & Nowcasting Framework

## The target and the constraint
We want **daily dollars at event grain**. Our dollar truth is coarse (county-quarter
CDTFA, county-week OI, annual/monthly TOT). So daily spend is *inferred*, and it can
only be **validated at the anchor's grain**: sum daily estimates over a quarter →
check vs CDTFA; over a month → check vs TOT. Below that grain the estimate is
principled interpolation carrying quantified-but-unverifiable uncertainty. Report
credible intervals; never claim the hourly/daily figure is "measured."

**One contingent exception — daily-grain validation.** Mastercard SpendingPulse
(daily SF-county dollars, pending AWS Data Exchange approval — see docs/01) is the one
source that could check the disaggregation *below* the quarterly sum: hold it out, then
compare our Chow-Lin/Denton **daily** series to Mastercard's actual daily county dollars
(correlation of daily shape, not just the exact quarterly total we already reconcile to).
We do **not** build on it — if it never arrives the framework is unchanged — but if it
lands it converts the daily estimate from "unverifiable below quarter grain" to
"validated against a real daily meter," which is the strongest single result the
disaggregation step could show.

## Velocity is a calibrated coefficient
    daily_spend ≈ presence(t) × velocity(visitor_share, category, event_type, tier)

Calibrate by regressing anchor dollars on presence aggregated to the anchor's grain;
the coefficient is average velocity; apply it back down at event grain. Velocity is
**not constant** — letting the model learn it as a function of visitor share,
category, event type, and county tier is why this is ML, not a fixed multiplier.

Anchor each spending **category** to its best dollar meter:
- dining/food → CDTFA C08 (+ city-level CDTFA)
- lodging → TOT + AirDNA (near-direct; strongest anchor; best in Tier-3)
- retail/entertainment → OI tracker categories

## The method ladder (build in this order — each generalizes the last)

**1. Temporal disaggregation (Chow-Lin / Denton).** Distribute a quarterly total
across days so daily values (a) sum back exactly to the observed total and (b) follow
a high-frequency indicator. The exact-summing (reconciliation) property is what we
want. Pure interpolation, no latent structure. Baseline sanity check.
Tooling: `tempdisagg`.

**2. MIDAS (Mixed Data Sampling).** Regress low-freq dollars on high-freq indicators
without spending 90 coefficients on 90 daily lags — the lag weights follow a smooth
2–3 parameter curve. Here **β on the indicator ≈ spending velocity**, and the weight
curve tells you *when within the period* the activity happens (front-loaded? lagged?).
Good for one target + a few predictors.

**3. Dynamic Factor Model (DFM).** Many noisy partial indicators (BART, PeMS,
wastewater, foot traffic) load on one latent "local economic activity" factor:
`x_it = λ_i f_t + e_it`. Estimated from the whole panel, so no single missing/noisy
indicator sinks it. In state-space form the Kalman filter simply skips missing
observations — **this is the fusion layer for our sparse, coverage-varies-by-event
matrix.** Tooling: `statsmodels.tsa.statespace.DynamicFactorMQ` (NY Fed methodology;
handles mixed frequency + ragged edge out of the box).

**4. State-space + Kalman filter (the container).** Measurement equation (observed =
f(latent state) + noise) + transition equation (state dynamics). 1–3 are special
cases. Connect quarterly CDTFA to daily latent spend via the **Mariano-Murasawa**
device: treat the quarterly figure as a daily variable observed only at quarter-end,
with an accumulation constraint that the observed quarterly value = sum of the
unobserved daily pieces. Filter interpolates daily spend consistent with both the
indicators and the summing constraint — and yields **uncertainty intervals for free**.

## Nowcast and causal are the SAME object
**BSTS / CausalImpact** (Brodersen et al.) is a state-space model: fit on the
pre-event window, forecast the counterfactual "no-event" path, take
observed − predicted as the effect. That is synthetic-control-in-the-time-domain —
the causal-identification upgrade flagged in AAI-540. Read the latent state as
"spend level" → it's a nowcast; read the post-event deviation → it's the event's
causal impact with credible intervals. The wastewater flow-residual model, the DiD/
synthetic-control plan, and this spend nowcast all collapse into one framework.

## Two honest caveats
1. **Short series.** These methods assume long low-frequency history to pin factor
   loadings. Per-county CDTFA is short. **The panel dimension substitutes for the
   time dimension** — pool across many counties/events. This is why leave-one-county-
   out isn't just validation; it's what makes estimation feasible.
2. **Spiky target.** Standard nowcasting targets a smooth aggregate; ours is a smooth
   baseline + a spiky event. Prefer the state-space-with-intervention (BSTS) form,
   which separates baseline factor from event shock explicitly.

## Suggested sequence for the capstone
Chow-Lin baseline (does distributing CDTFA by a presence proxy give sane daily
numbers?) → MIDAS (express velocity explicitly on one anchor) → `DynamicFactorMQ`
(fuse all partial-coverage indicators) → read the same model in BSTS mode for the
causal estimate. Each step is a strict generalization → clean narrative arc for the report.
