# 05 — Data Access Guide

*Team onboarding reference: every data source this project touches — what it is, what
we use it for, how to access it, its limits, and the code entry point. Written
2026-07-04. If a source isn't listed here or in `config/sources.yaml`, register it
there (with license + status) before writing an ingester.*

> **Credentials note:** this guide contains real tokens/logins (team decision — all
> free-tier APIs, no billing exposure). Keep the repo private. If a credential stops
> working, regenerate it and update this doc + your `.env`.

---

## 1. Quickstart (any teammate, ~10 minutes)

```bash
git clone <repo> && cd eia-nowcast-pipeline
uv sync                          # Python 3.11 env, all deps
cp .env.example .env             # then fill values from §2 below
uv run python -c "from eia_pipeline.settings import settings; print(settings.summary())"
```

`settings.summary()` prints which credentials are SET without printing values.
Then smoke-test the keyless core (no credentials needed):

```python
from eia_pipeline.ingest.mlb import fetch_home_schedule
from eia_pipeline.ingest.bart import fetch_od

print(fetch_home_schedule(2024).head())   # ~2s — Giants home slate
print(fetch_od(2024).head())              # ~1 min first time (~80MB), cached after
```

If both print DataFrames, you are fully operational on the working pipeline.

**`.env` formatting gotcha:** put comments on their **own line**. python-dotenv
includes trailing `# comments` in the parsed value and silently corrupts it.

## 2. Credentials

| Env var | Source | Value |
|---|---|---|
| `PEMS_USERNAME` | pems.dot.ca.gov | `lucasyoung@sandiego.edu` |
| `PEMS_PASSWORD` | pems.dot.ca.gov | `Cx7g&waterw` |
| `CENSUS_API_KEY` | api.census.gov | *(not yet registered — free, instant)* |
| `NOAA_NCEI_TOKEN` | ncdc.noaa.gov/cdo-web | *(not yet registered — free, instant)* |
| `SEATGEEK_CLIENT_ID` | platform.seatgeek.com | *(not yet registered)* |

PeMS tip: accounts are free (1–2 business-day approval). If several people will pull
heavily at once, register your own — simultaneous shared logins from many IPs can
trip their abuse detection.

---

## 3. Working sources (ingesters exist and run)

### 3.1 BART hourly origin-destination — the presence workhorse

- **What/why:** station-to-station hourly exit counts. Our primary crowd signal:
  differenced against same-weekday baselines it yields the per-game arrival uplift
  (calibrated: EMBR sees ~3.5% of the Giants gate, linearly).
- **Access (keyless):** one gzipped CSV per year, **no header row**:
  `https://afcweb.bart.gov/ridership/origin-destination/date-hour-soo-dest-{year}.csv.gz`
  Station code → name/lat-lon via the GTFS feed:
  `https://www.bart.gov/dev/schedules/google_transit.zip` (`stops.txt`).
- **Use:** `fetch_od(year)`, `fetch_stations()` in `src/eia_pipeline/ingest/bart.py`.
  Columns: `date, hour (0-23, local Pacific), origin, destination, trip_count`.
- **Limits/quirks:** ~8.9M rows/year (~80MB gz) — first pull is slow, then cached
  under `data/raw/bart/`. Coverage 2018–2026. Counts are *exits*, station-pair grain;
  EMBR/MONT are transfer stations in the financial district, not the ballpark gate.
- **License:** CC-BY.

### 3.2 MLB Stats API — event catalog + attendance (ground truth)

- **What/why:** the *treatment*: which days had a home game, exact first-pitch time,
  opponent, and per-game attendance (the crowd ground truth we calibrate against).
- **Access (keyless):** `GET https://statsapi.mlb.com/api/v1/schedule` with
  `sportId=1, teamId=137, startDate, endDate, gameType=R`. Attendance via the
  boxscore endpoint, joined on `game_pk`.
- **Use:** `fetch_home_schedule(season)` in `src/eia_pipeline/ingest/mlb.py`.
- **Limits/quirks:** `gameDate` is **UTC** — the ingester converts to
  `America/Los_Angeles` before deriving game date / first-pitch hour (BART is in
  local time; misaligning this quietly breaks the differencing).
- **License:** public MLB endpoint; personal/non-commercial use, attribute MLB.
  Do **not** substitute Sports-Reference (ToS-restricted — see D9).

### 3.3 CDTFA taxable sales (OData) — the dollar anchor

- **What/why:** quarterly taxable sales by county × business group, **business-location
  basis** (reported where the transaction happened) — the correct geographic basis for
  visitor spend and therefore *the* dollar anchor. SF food-services (C08) drives the
  daily disaggregation ($12.5M/day scale).
- **Access (keyless):** OData REST —
  `https://cdtfa.ca.gov/dataportal/api/odata/Taxable_Sales_Counties`
  (use the **apex host**; `www.` 301-redirects). Supports `$filter/$orderby/$top`,
  pages via `@odata.nextLink`. Our filter:
  `County eq 'SAN FRANCISCO' and BusinessGroupCode eq 'C08'`.
- **Use:** `fetch_county_taxable_sales()` / `fetch_food_services()` in
  `src/eia_pipeline/ingest/cdtfa.py`. Dollars are whole USD in `TaxableTransactions`.
- **Limits/quirks:** quarterly, ~2-quarter lag, older quarters may be revised. Rows
  carry a `DisclosureFlag` (small-cell suppression; never triggered for SF). City
  grain: use `Taxable_Sales_Cities` (carries C08), **not** `Taxable_Sales_by_City`
  (Retail+Food combined only). SF is a consolidated city-county, so city grain adds
  nothing for SF. Coverage: 2015 Q1–2025 Q4, contiguous.
- **License:** public.

### 3.4 Opportunity Insights Economic Tracker — high-frequency %-lift

- **What/why:** daily county card-spend index. Supplies the high-frequency *shape/lift*
  (our +1.16pp per 10k attendees came from it); dollars come from CDTFA.
- **Access (keyless):** raw CSV on GitHub:
  `https://raw.githubusercontent.com/OpportunityInsights/EconomicTracker/main/data/Affinity%20-%20County%20-%20Daily.csv`
  SF county FIPS `6075`.
- **Use:** `county_daily_spend()` in `src/eia_pipeline/ingest/oi_tracker.py`.
  `spend_all` is a fractional deviation vs Jan-2020 (0.044 = +4.4%).
- **Limits/quirks (both decisive):** county **daily** rows end ~2022-06 (weekly
  after) — daily calibration is confined to the 2021 + early-2022 slate. And the
  series is **residence-based** (cardholder's home ZIP, not the merchant): it measures
  SF residents' spending anywhere, *not* spending in SF. Use for %-shape only; never
  dollarize it directly.
- **License:** public; cite Opportunity Insights + the tracker.

### 3.5 Caltrans PeMS — freeway flow (keyed, ingester not yet written)

- **What/why:** loop-detector vehicle counts (flow/occupancy/speed) at 5-min grain,
  ~10-year archive. Per decision **D4**: for downtown SF venues this is a **regional
  covariate** (control in the velocity regression, feature for the presence model),
  *not* a venue signal — the loops nearest Oracle Park carry all city-bound traffic.
- **Access (keyed):** `https://pems.dot.ca.gov/` — **web login + session cookie**,
  not an API key (credentials in §2). Data via the Data Clearinghouse: bulk
  per-district text files (station 5-min / hourly / daily + VDS metadata).
  District 4 = Bay Area (~4,600 detectors).
- **Use:** not yet wired; will land as `src/eia_pipeline/ingest/pems.py` reading
  `settings.pems_username` / `settings.pems_password`.
- **Limits/quirks:** counts **vehicles, not people** (needs a calibrated
  persons/vehicle factor like every proxy); clearinghouse files are large — pull per
  district×day and cache.
- **License:** public (Caltrans).

---

## 4. Registered, not yet wired (stubs or backlog)

| Source | Role | Access | Status |
|---|---|---|---|
| **Open-Meteo archive** | weather controls (precip, soil moisture, ET, temp) for confounders + wastewater residual | keyless: `GET https://archive-api.open-meteo.com/v1/archive` (`latitude, longitude, start_date, end_date, daily=, hourly=, timezone`); ERA5-Land ~9km, 1950–present | stub `ingest/openmeteo.py`. ≤10k calls/day free non-commercial; CC-BY-4.0 |
| **CA eSMR wastewater** | presence proxy: treatment-plant flow residual | keyless: data.ca.gov CKAN ("Water Quality – Effluent – eSMR"), DataStore SQL API or bulk files; filter `parameter=flow` + venue-sewershed facility | stub `ingest/esmr.py`. Daily at best (permit-dependent); weather dominates — needs Open-Meteo controls |
| **TOT lodging tax** | annual lodging dollar anchor | keyless CSV (CKAN): data.ca.gov "City TOT FY2017–2024" | backlog; too coarse for event grain, later category anchor |
| **Wikipedia pageviews** | pre-event attention/anticipation | keyless REST: `https://wikimedia.org/api/rest_v1/` Pageviews API | backlog |
| **Census ACS** | demographic denominators | free API key: `https://api.census.gov/data` | backlog; key not yet registered |
| **NOAA NCEI** | gauge ground-truth for Open-Meteo (optional) | free token: ncdc.noaa.gov/cdo-web | optional; reintroduces missing-data gaps |
| **SeatGeek** | ticket listings/prices (willingness-to-pay) | free API key: platform.seatgeek.com | backlog; review commercial terms first |

**Ruled out / paid (do not build):** OI Womply business-location series (weekly,
coverage ends 2022-02 — verified unusable); Advan/Dewey foot traffic (D2 — channel
closed); sub-meter satellite & AirDNA (paid-inquire track); anything ToS-restricted —
Sports-Reference, live OpenTable/Resy scraping, location-broker data (D9).

---

## 5. Where data lands

- Raw pulls cache under `data/raw/<source>/` (gitignored) — every ingester downloads
  once and reuses the cache; delete a file to force a re-pull.
- Processed tables land as Parquet via `src/eia_pipeline/io.py` (`land_parquet`),
  local-first; S3 mirroring uses `settings.s3_uri()` once `S3_BUCKET` is set.
- Query locally with DuckDB/Polars (project convention — no warehouse needed for EDA).

## 6. House rules (from CLAUDE.md — settled)

1. Register a source in `config/sources.yaml` (license + status) **before** writing
   an ingester.
2. Presence ≠ people ≠ dollars — every proxy gets an explicit, *calibrated*
   conversion factor, kept in `calibrate/`.
3. Respect ToS: no scraping restricted sources (§4 ruled-out list).
4. AWS work: boto3-first; pin `sagemaker<3` if the SDK is unavoidable; Lambda-first
   for ingestion (ml.m5.large is quota-constrained).
