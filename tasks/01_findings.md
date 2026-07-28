# Task 01 findings — BART pre-game arrival uplift at Oracle Park (EMBR)

**Status: GREEN.** The ingest → land → query → event-day difference loop runs end to end,
keyless, and returns a clear positive signal.

## What was built
- `ingest/bart.py` — BART hourly OD (`date-hour-soo-dest-YYYY.csv.gz`, ~8.9M rows/yr) +
  keyless GTFS station-code lookup. Native trip counts, Parquet-landed.
- `ingest/mlb.py` — MLB Stats API event catalog (keyless). Giants home games with
  first-pitch time converted UTC → America/Los_Angeles to align with BART's local time.
- `transform/event_study.py` — per-game arrival uplift vs same-day-of-week non-game
  baseline over the pre-game window.

## Method
For each 2024 Giants **night** home game (46 of 81), sum BART arrivals at **EMBR**
(destination = EMBR) over the 3-hour pre-game window `[first_pitch-2, first_pitch]`, and
subtract the mean arrivals on **non-game days of the same weekday** over the same clock
hours. Uplift = game − baseline.

## Result (2024, EMBR, night games, 3h window)
| metric | value |
|---|---|
| games | 46 |
| mean uplift | **~1,118 net arrivals** |
| std / SE | 377 / 55.6 |
| t-stat | ~20 |
| games with positive uplift | **100%** |

Ranking is face-valid: largest uplifts are the **A's Bay Bridge series** and
**Dodgers / Padres / Pirates** (marquee draws); smallest are midweek
**Nationals / Brewers / Diamondbacks**.

## Honest caveats (these define the next steps, not footnotes)
1. **EMBR is a transfer mode, not the front door.** ~1,118 net arrivals against a
   ~30–40k gate ⇒ BART-via-EMBR sees only ~3% of attendance. This is *presence, a
   partial one* — never mistake it for the crowd. The front-door mode is **Muni Metro
   (N/T lines)** → the required fast-follow ingester.
2. **Night games only.** Day games (first pitch 12–14h) overlap the midday/commute
   pattern and need a different window; excluded from this v1.
3. **Single station.** MONT (Montgomery) is the other plausible downtown transfer point;
   adding it would widen coverage.
4. **Baseline contamination.** "Non-game" weekdays may include other downtown events
   (concerts, conventions), which would *understate* true uplift — the signal is a
   conservative floor.
5. Not weather- or holiday-adjusted yet.

## Reproduce
```python
import polars as pl
from eia_pipeline.ingest import bart, mlb
from eia_pipeline.transform.event_study import bart_arrival_uplift
od = bart.fetch_od(2024)
sch = mlb.fetch_home_schedule(2024)
per_game, summary = bart_arrival_uplift(od, sch, station="EMBR")
```

## Follow-up measurements (2026-07-01)

### Attendance calibration — DONE (first Stage-D coefficient)
`calibrate/bart_attendance.py`. Joined the 46-game uplift to MLB gate attendance
(keyless, `hydrate=gameInfo`).

| metric | value |
|---|---|
| corr(attendance, uplift) | **0.693** |
| OLS slope | **~54 net BART arrivals per +1,000 fans** |
| OLS intercept | −578 |
| mean / median capture rate | **3.49% / 3.46%** of the gate |

Read: BART-at-EMBR captures ~3.5% of Oracle Park attendance, and that capture scales
with the gate (r≈0.69). This is the quantified "transfer mode" caveat — the number the
nowcast needs, and the reason a front-door signal is still wanted. (Negative intercept:
at low attendance the same-DOW baseline slightly over-subtracts — revisit with a
first-pitch-relative baseline.)

> **SUPERSEDED 2026-07-09 — Muni scrapped (see docs/03 D12). The Muni ingester, capture job, and ops/ files referenced below were removed; this section is retained as a historical record only.**

### Muni front-door signal — NOT AVAILABLE keyless for 2024 (documented, not a dead end)
Investigated DataSF + 511 thoroughly:
- No queryable historical **stop-hour** Muni boardings dataset (only route/week
  aggregates; the one APC raw dataset `im6q-3pc9`, Jul–Dec 2021, is a non-queryable file
  blob).
- 511.org is **real-time GTFS-RT only** — no archive.

⇒ Muni is a **forward-capture** signal (poll 511 for future games) or a direct SFMTA APC
request. **Do not re-hunt a keyless 2024 backfill — it does not exist.**

**BUILT + verified live (511 token now in `.env`):** `ingest/muni.py` polls the gate
stops (King & 2nd, N/T platforms 15234–15237) and returns per-vehicle **Occupancy**
(`seatsAvailable`→`full`, scored 0–4) + arrival predictions. NOT APC boardings — a
crowding/throughput proxy. `transform/muni_signal.py` reduces accumulated polls to a
per-evening gate signal.

**Automated capture:** `scripts/capture_muni.py` + a macOS LaunchAgent
(`ops/launchd/install.sh`) poll every 5 min within 10:00–20:00 PT, landing
`data/raw/muni/capture_*.parquet`. **Runs on the always-on Mac Studio** (see `ops/README.md`),
not a laptop. Every evening captured → non-game evenings are the baseline. First game to
land: **2026-07-06 18:00 vs Toronto**.

## Next
- Let Muni capture accumulate on the Studio through ≥1 home game + baseline evenings, then
  run `evening_signal` and difference game vs non-game (front-door counterpart to the
  3.5% EMBR capture rate).
- Extend BART study to **day games** (first-pitch-relative window) and add **MONT**.
- Use the capture coefficient as the first input to the velocity calibration (Stage D→E).
