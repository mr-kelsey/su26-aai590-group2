# 01 — Data Acquisition Strategy

Organize proxies by **where they sit in the event's economic funnel**. Each phase
illuminates a different temporal slice, so the signals are complementary, not redundant.
Machine-readable status for every source is in `config/sources.yaml`.

## Two data homes (read this first)

Data now lives in two places, and they overlap:

1. **This repo's `build-now` ingesters** — keyless public pulls we wrote and control
   (`src/eia_pipeline/ingest/`). Land to local Parquet / our S3 prefix.
2. **The team Gold warehouse** — `s3://aai-590-group2-capstone/Gold/` — a curated,
   SF/Giants-scoped layer built in parallel. It **duplicates** several of our sources
   (BART, CDTFA, OI, MLB, weather — independently rebuilt) and **adds** ones we don't
   have (Advan Patterns, competing-events confounders, derived $/visit inputs, Bay
   Wheels, Mastercard-pending). Access from this workspace is via SageMaker processing
   jobs under the execution role (our `lucas` IAM user can list but not read objects);
   see `docs/05_data_access_guide.md`.

**Convergence action:** treat the Gold warehouse as the source of truth for the
SF/Giants corpus and reconcile our repo ingesters to it rather than maintaining two
divergent copies. Where the warehouse is fresher or fixes a dead endpoint (see BART
below), prefer it.

## Phase 1 — Anticipation (days–weeks before)
- **Wikipedia Pageviews API** — free, keyless, daily attention proxy.
- **Ticketmaster Discovery / SeatGeek** — event metadata + ticket pricing. Gold holds
  `competing_events/ticketmaster_events.csv` (upcoming-only, 2026-07+).
- **AirDNA** — short-term-rental price/occupancy; forward spikes = anticipation AND a
  lodging-dollar signal (paid, affordable). Not yet acquired.

## Phase 2 — Inflow (hours before / day-of)
- **BART** — still the best Bay-Area inflow signal, but the feeds changed:
  - **Daily station exits** (Gold `bart_daily_exits/`, DataSF `m2xz-p7ja`) — 1998-01-01
    to **2025-07-31**, then STALE. Covers most of the study window at daily grain; the
    game-day vs non-game contrast comes from here.
  - **Monthly OD workbooks** (Gold `bart_monthly_od/`) — the only post-2025-07
    continuation, at monthly grain. Behind a Cloudflare challenge (browser-fetched).
  - ⚠️ **The hourly OD host is DEAD.** `afcweb.bart.gov` / `64.111.127.166`
    (`date-hour-soo-dest-YYYY.csv.gz`) — the URL in our `ingest/bart.py` — went dead
    ~2026-07-02. Our hourly-OD path no longer runs; the 2024 hourly pull we already
    landed is still valid history, but there is no public hourly successor. **Fix our
    ingester to read the Gold daily-exits + monthly-OD instead.**
- **Caltrans PeMS** — freeway loop **flow**, 5-min, District 4 (~4,600 loops). Free
  account (now held, creds in `.env`). Regional covariate for downtown SF venues, not
  last-mile (decision D4).
- **Bay Wheels bike-share** (Gold `baywheels_bikeshare/`) — per-trip start/end lat-lng +
  timestamps, 2017→present. A point-level last-mile presence signal near the park; also
  the stand-in for the retired DataSF taxi feed.

## Phase 3 — On-site activity (during)
- **Advan Patterns foot traffic — NOW IN (reverses decision D2; see D10).** Licensed
  access via the capstone bucket. Weekly per-POI visits, 2020-01→2026-05, with
  `VISITS_BY_EACH_HOUR` (168 hourly), `VISITS_BY_DAY`, dwell distributions, and
  `VISITOR_HOME_CBGS` (visitor origin). We extracted the **Oracle Park 1 km ring**
  (490 POIs incl. the park itself ~24k visits/wk, and 20 NAICS-722 restaurants/bars) to
  `data/raw/advan/`. This is the venue-grain presence signal that replaces the ~3.5%
  BART-through-EMBR proxy, and its visitor-origin field feeds the `visitor_share`
  segmentation velocity needs.
- **Wastewater flow residual** — net imported visitors (decision D5). Not yet built.
- **Satellite lot-fill** — Sentinel-2; isolated-lot daytime venues only. Useless for
  Oracle Park (D3). Deprioritized for this venue.

## Phase 4 — Settlement (the dollar anchors)

Anchor hierarchy by grain. **CDTFA is the primary settlement anchor we build on;**
Mastercard is a contingent evaluation target, not a build dependency (see below).

- **CDTFA taxable sales — the quarterly settlement anchor.** SF county×quarter,
  business-location basis, C08 (Food Services) isolable; held in Gold + our OData
  ingester. SF is a consolidated city-county (FIPS 06075) so county grain = city grain.
- **Opportunity Insights Affinity — the weekly %-shape anchor.** Residence-based index
  (not dollars, not merchant-location); frozen at 2024-06. Held Gold
  `opportunity_insights/oi_affinity_sf_daily.csv`. Use for shape only.
- **Derived $/visit bridge** (Gold `derived_spend_inputs/`) — Economic Census 2022
  receipts-per-establishment by NAICS × FRED retail shape ÷ SF CPI, calibrated so ring
  aggregates match CDTFA quarterly. This is what converts Advan ring **visits** into
  sub-county **dollars** — the bottom-up counterpart the county anchors validate
  top-down. It IS the `spending_velocity` calibration made concrete.
- **TOT** — annual lodging dollars; too coarse for event grain, later category anchor.

**Mastercard SpendingPulse — a contingent EVAL target, not an anchor we build on.**
AWS Data Exchange, free but approval-gated; requested 2026-07-01, may not arrive in
time and **we do not plan around it**. Daily SF-county (06075) all-payment **dollars**
back to 2018, 12 sectors incl. **Restaurants**. Its value if it lands is as *held-out
validation of the temporal disaggregation itself*: our Chow-Lin/Denton series is
currently only checkable at the quarterly sum (docs/02), so a true daily county-dollar
series is the one source that can test whether our **daily** shape is right, not just
the quarterly total. So: nothing depends on it; if it arrives, it upgrades from
"anchor" to "the eval that makes the daily estimate defensible below quarter grain."

## Confounder / control layer (new — needed for clean identification)
- **Competing events** (Gold `competing_events/`): Warriors + WNBA Valkyries at Chase
  Center (0.74 mi), Moscone citywide conventions (114 large editions 2016–2026), and
  54 ballpark NON-baseball event-days (concerts, Monster Jam, soccer). Two uses:
  (1) **control** for overlapping downtown demand in the velocity regression;
  (2) **clean the non-game control pool** by excluding ballpark-event days that would
  otherwise contaminate the game-vs-non-game contrast. Also the **multi-venue expansion
  path** (Chase Center shares BART geography) when we pool beyond Giants games.
- **Weather** — Gold `noaa_weather/` (SF daily 2016–present, PRCP/TMAX/TMIN/AWND) +
  our Open-Meteo ERA5-Land client. Confounder control + wastewater residual driver.
- **Census ACS / Economic Census** — CBG denominators + the NAICS receipts table above.

## Tiers (drive build order — mirror in sources.yaml `status`)
- **have-now** (in Gold or already landed local): Advan ring, BART daily-exits/monthly,
  CDTFA, OI, MLB schedule+attendance, NOAA weather, competing-events, derived-spend
  inputs, Bay Wheels.
- **build-now** (free, keyless, ours): Open-Meteo, CA eSMR, Wikipedia pageviews.
- **key-needed** (free account/token, held): PeMS. (Census, NOAA NCEI — not yet.)
- **contingent-eval** (may not arrive; not planned around): Mastercard SpendingPulse
  (AWS Data Exchange approval) — held-out daily-grain validation if it lands.
- **paid-inquire**: AirDNA, commercial sub-meter satellite.

## Coverage varies by event — design consequence
No proxy is statewide; even within SF, sources start/stop at different dates (Advan
2020+, OI ends 2024-06, BART daily ends 2025-07, Mastercard 2018+). **We accept a
sparse, ragged-edge feature matrix** and let the model handle missing channels — the
state-space / dynamic-factor form estimates the latent activity factor from whatever
indicators exist per date. Do **not** impute signals that don't exist for a given
date/venue to force a dense matrix.
