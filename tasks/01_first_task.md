# Task 01 — First slice: wastewater flow residual for one facility + one event

**Goal:** prove the ingest → land → query → residual loop end-to-end, with **zero
credentials**, and see whether a weather-adjusted flow residual actually moves on a
known event day. This is the smallest thing that exercises the whole spine.

## Why this task
- Uses only `build-now` sources: **CA eSMR** (flow) + **Open-Meteo** (weather).
  No keys, no approvals.
- Has a visible pass/fail (does the residual move on event day?).
- Establishes the patterns everything else reuses: env-driven config, Parquet landing,
  DuckDB query, native-unit storage, honest caveats.

## Steps
1. **Pick the case.** Choose one Tier-3 city with a large, dateable, multi-day event
   and a single treatment plant serving it (candidates in `venue_crosswalk.csv`, e.g.
   Indio / Coachella weekend). Resolve the eSMR `facility` + the plant `lat/lon`.
2. **Ingest flow** (`ingest/esmr.py`): pull the facility's daily `flow` series for a
   window spanning several months around the event (need baseline + event + control days).
   Land to Parquet.
3. **Ingest weather** (`ingest/openmeteo.py`): pull daily `precipitation_sum`,
   `et0_fao_evapotranspiration`, `temperature_2m_mean`, `snowfall_sum`, and daily-mean
   `soil_moisture_7_to_28cm` (+ a deeper band) for the plant lat/lon, same window.
4. **Join** on facility-date (`transform/`), land the joined table.
5. **Residualize** (`transform/residual.py`): regress flow on
   precip + antecedent soil moisture + ET + temp + day-of-week + month.
   Also compute a derived Antecedent Precipitation Index from the precip series.
   Keep the **residual** (flow above weather-predicted).
6. **Check** in DuckDB: does the residual spike on the event day (and day+1, given
   sewer travel-time lag)? Difference event-day residual vs comparable non-event days
   at the same facility. Report the number honestly — a null result is a valid finding.

## Pass criteria
- Two ingesters return tidy DataFrames in native units, landed as Parquet.
- A joined flow+weather table exists and is queryable via DuckDB.
- A residual series is produced and the event-day vs baseline comparison is reported
  with its uncertainty. (We are testing the *method*, not hoping for a big number.)

## Do NOT
- Do not convert flow to people yet (that's a later calibration step — persons-per-
  contribution factor fit against known attendance).
- Do not add other sources until this loop is green.
- Do not hardcode the facility/coords — put them in config or a small run script arg.

## Alternative first task (equally clean, also keyless)
**BART hourly-OD pull for one Bay Area venue** (`bart_hourly_od`): download one year's
hourly OD file + station lookup, filter dest = venue station, and difference event-day
hourly exits vs comparable non-event days. Same loop, transit flavor. Pick whichever
matches the first corpus event you want to work.
