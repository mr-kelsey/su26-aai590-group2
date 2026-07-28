# Backlog (sequence after Task 01)

Build in tiers. Get each stage green before the next. Prefer small PRs.

## Stage A — ingest the free/keyless anchors + inflow (build-now)
- [ ] `cdtfa_c08` — county-quarter (and city-level) taxable sales into the anchor table.
- [ ] `oi_economic_tracker` — county-week spend by category. **Verify coverage end-date first.**
- [ ] `tot_annual_statewide` — city/county TOT (lodging anchor).
- [ ] `bart_hourly_od` — Bay Area inflow (if not done as Task 01 alt).
- [ ] `wikipedia_pageviews` — anticipation.
- [ ] `open_meteo` — reusable weather client (done partly in Task 01).

## Stage B — the crosswalk (the join glue; highest-leverage, hardest)
- [ ] Fill `venue_crosswalk.csv` for the first ~10 corpus venues: county_fips,
      cdtfa_city, lat/lon, sewershed plant id, nearest BART station, PeMS VDS ids.
- [ ] Build the event catalog join (event_id ↔ venue_id ↔ date ↔ announced_attendance).

## Stage C — keyed sources (once accounts exist)
- [ ] `pems_flow` — District 4 + District 7 VDS near corpus venues (regional covariate).
- [ ] `census_acs` — CBG denominators.
- [ ] `sentinel2_lotfill` — isolated-lot daytime venues only (Coliseum-type).

## Stage D — calibration (velocity)
- [ ] Fit wastewater flow-residual → attendance factor against known-attendance events.
- [ ] Fit dollars/person-hour velocity: regress anchor dollars on aggregated presence
      at anchor grain; segment by visitor_share / category / event_type / tier.
- [ ] Anchor each category to its meter (food→CDTFA, lodging→TOT/AirDNA, retail→OI).

## Stage E — nowcast (the method ladder; see docs/02)
- [ ] Chow-Lin/Denton reconciliation baseline (`tempdisagg`).
- [ ] MIDAS regression on one anchor (velocity + timing weights).
- [ ] `DynamicFactorMQ` fusion of all partial-coverage indicators.
- [ ] BSTS / CausalImpact read of the same state-space model for the causal estimate.

## Stage F — validation & generalization
- [ ] Reconcile daily estimates to CDTFA (quarter) and TOT (month).
- [ ] Leave-one-county-out and leave-one-event-type-out CV.
- [ ] Direct → total impact via multiplier layer (kept separate; D7).

## Paid / inquiry (parallel track, not blocking)
- [ ] AirDNA (lodging dollars, event-day). [ ] Spectus academic (foot-traffic substitute).
- [ ] Sub-meter satellite for a small validation subset (amortized precision).
