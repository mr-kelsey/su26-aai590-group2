# 04 — Data Exploration: findings and options

Concise record of every exploration to date: what was tried, what it found, and the
options forward. The full narrative log lives in this file's git history.

Last updated: **2026-07-01** · Focus venue: **Oracle Park (SF Giants)**, night games 2024
unless noted.

## The question

A dollar figure for the local economic impact of a single event on a single day. No
source measures this, so it is inferred (nowcast):

```
daily_spend(event) ≈ presence(t) × spending_velocity(segment, category, place)
```

- **presence** — high-frequency crowd proxies (transit, traffic, wastewater).
- **velocity** — dollars per unit of presence; never assumed, always calibrated against
  slow dollar anchors (CDTFA, TOT).

Daily estimates can only be *validated* at the anchor's grain (quarter, month); below
that they are principled interpolation with quantified uncertainty.

---

## Explorations and findings

### 1. BART pre-game arrival uplift — presence signal ✅

- **Data:** BART hourly origin-destination (keyless, 2018–2026 available; 2024 used) +
  Giants schedule from the MLB Stats API (keyless).
- **Method:** sum arrivals at Embarcadero (EMBR) in the 3 hours before first pitch;
  subtract the mean of the same clock hours on non-game days of the same weekday. The
  residual is game-attributable uplift.
- **Found:** all 46 night home games positive; mean **~1,118 net arrivals**, t ≈ 20.
  Opponent ranking is face-valid (A's, Dodgers, Padres top; midweek low-draw teams bottom).
- **Lesson:** levels are drowned in commuters; **differencing against a matched baseline
  is the method**, reused everywhere since.

### 2. BART ↔ attendance calibration ✅

- **Method:** regress per-game uplift on actual MLB gate attendance.
- **Found:** r = **0.69**; ~**54 net arrivals per 1,000 fans**; mean **capture rate ~3.5%**
  of the gate, scaling linearly with crowd size.
- **Meaning:** first calibrated conversion factor. EMBR (a transfer station, not the
  front door) sees a small but stable fraction of the crowd.

### 3. Crowds → dollars via OI card spend — method proven, number not reportable ⚠️

- **Method:** regress the Opportunity Insights SF-county **daily** spend index on
  attendance-per-10k with day-of-week/month/year controls, on 2021–early-2022 (COVID
  capacity limits varied attendance 3.7k–41k — useful dose-response).
- **Found:** **+1.16pp of SF-wide daily card spend per 10,000 attendees** (95% CI
  [0.51, 1.82], p = 0.0005); ~+2.7% at mean attendance. Naively dollarized:
  $87–145/attendee — **biased high** vs the ~$30–70 the event-impact literature finds.
- **Decisive caveat:** OI is **residence-based** (cardholder's home ZIP, not the
  merchant's). It measures *SF residents' spending anywhere*, not *spending in SF* —
  largely the wrong side of the ledger for visitor-spend impact. The percent lift is
  real and calibrated; the dollar figure is an envelope only.

### 4. Anchor basis reckoning — residence vs business-location ✅

- OI's business-location alternative (Womply small-business revenue) was checked and
  ruled out: right basis, but **weekly** grain and coverage ends **Feb 2022** (entirely
  fanless/capacity-limited COVID) — no identifying variation. Clean negative.
- **Conclusion that reorganized the plan:** CDTFA (sales tax) and TOT (lodging tax) are
  **business-location based** — the correct geographic basis for visitor spend. CDTFA is
  therefore *the* dollar anchor, not a units fix. Geographic attribution is as decisive
  a property of a spend source as grain or coverage.

### 5. CDTFA dollar anchor ✅

- **Found:** keyless **OData API** (an earlier "portal spreadsheets only" note was
  wrong). SF **food-services (C08)** quarterly dollars, **2015 Q1–2025 Q4**, contiguous,
  business-location basis, no suppression.

### 6. Temporal disaggregation — reconciled daily dollars ✅ baseline

- **Tooling finding:** off-the-shelf `tempdisagg`/statsmodels **silently corrupt the
  sum-back** on irregular quarter→day ratios (up to 2.4% error). Wrote a direct numpy
  Denton + Chow-Lin with an explicit aggregation matrix
  (`src/eia_pipeline/nowcast/disaggregate.py`); reconciles to ~1e-15.
- **Result:** daily SF restaurant taxable sales, **~$12.5M/day** (~$4.6B/yr), summing
  back to every CDTFA quarter exactly. Chow-Lin per-day SE ~**31%** — honestly wide.
- **Not claimed:** the naive game-day "premium" (~$223k/day) is **not a causal effect**
  — disaggregation *distributes* the quarterly total by the indicator's shape; it does
  not *attribute*. Also, the BART indicator is commute-weighted (weekday-heavy) while
  restaurant spend skews evenings/weekends, so the daily shape is imperfect even though
  the quarterly sum is exact.
- **Deliverable:** the reconciled daily dollar **scale** — the honest denominator that
  separately-calibrated event effects attach to.

---

## Source ledger

Status: ✅ used & working · ⏳ in progress · ❌ blocked · — not yet touched.

| Source | Role | Grain / cadence | Coverage | Status | Verdict |
|---|---|---|---|---|---|
| BART hourly OD | presence (historical) | station×hour | 2018–2026 | ✅ | Clean, keyless, the workhorse. EMBR = transfer station, ~3.5% capture. |
| MLB Stats API | event catalog + attendance | game | multi-year | ✅ | Keyless, authoritative; gives first-pitch time + attendance. |
| BART GTFS | station lookup | station | current | ✅ | Keyless name/lat-lon lookup. |
| OI Affinity (county) | spend anchor (%) | county×day | daily ends ~2022-06 | ⚠️ | **Residence-based.** High-freq %-lift; measures resident behavior, not in-SF spend. |
| OI Womply (county) | business-loc spend (%) | county×week | ends 2022-02 | ❌ | Right basis, wrong grain, all-COVID coverage. Unusable for events. |
| CDTFA C08 | spend anchor ($ level) | county×quarter | 2015Q1–2025Q4 | ✅ | Keyless OData; business-location. **The correct dollar anchor.** |
| TOT (annual) | lodging anchor | city/county×year | — | — | Too coarse for event grain; later. |

---

## Options moving forward

**Frame (capstone):** an end-to-end ML pipeline (deployment, monitoring, CI/CD,
registry) built around a **cascade**:

```
impact(future event) = predicted_presence(event features) × calibrated_velocity($/presence)
```

- The **presence side is the ML problem**: BART OD 2018–2026 gives hundreds of Giants
  home games plus every non-game day as baseline — enough to train a supervised model
  predicting crowd inflow from event features (opponent, day-of-week, first-pitch time,
  weather, month, standings). This is the deployable model.
- The **dollar side stays econometric**: ground truth is ~44 quarterly observations.
  Training an ML model on the *disaggregated* daily series would launder interpolation
  back in as data — it would learn Chow-Lin's indicator shape, not economics. Velocity
  is calibrated at the grain where truth exists and carried with intervals.

Ordered options (A blocks the dollar claim; C is the pipeline spine):

- **A. Attribution.** BSTS/CausalImpact on the disaggregated daily series: fit the
  no-game counterfactual, read game-day deviation as the causal effect with credible
  intervals. Converts the $12.5M/day scale + calibrated lift into the first defensible
  per-game dollar number.
- **B. Dining-appropriate indicator.** Evening/weekend-weighted or blended daily
  indicator (BART evening arrivals) so the Chow-Lin daily shape matches restaurant
  behavior. Cheap; improves everything downstream.
- **C. Event → presence model.** Gradient-boosted baseline on event features →
  per-game uplift; optional upgrade to a spatiotemporal GNN on the BART OD graph
  (stations = nodes, flows = edges). Expand training data: 2018–2025, day games,
  Montgomery station.
- **D. Widen events** (Oracle Park concerts and/or Chase Center — same transit
  geography, adds event-type variation). *Pending team decision;* Giants-only stands
  for now (homogeneity + sample size).
- **E. More spend categories.** CDTFA retail/bars; TOT lodging. One calibrated velocity
  per category.
- **MLOps wrapper.** Lambda-first ingestion, SageMaker (boto3, `sagemaker<3`) for
  train/deploy of the presence model, drift monitoring against incoming BART data,
  automated retrain-evaluate-promote loop (the shrunk version of the agent-refinement
  idea).

**Settled:** direct visitor spend only (no multipliers, D7); GNN optional, not
required; validate only at anchor grain.

---

*Artifacts: `tasks/01_findings.md` (presence + capture) (01 Muni section superseded — D12), `tasks/02_findings.md`
(crowds→dollars), `config/sources.yaml` (registry + verified coverage), and the
`ingest/`, `calibrate/`, `transform/`, `nowcast/` modules.*
