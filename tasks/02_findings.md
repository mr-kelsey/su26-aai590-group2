# Task 02 findings — first swing at CROWDS → DOLLARS (spending velocity)

**Status: first calibrated crowd→dollar link, with honest bounds.** This is doctrine
rung 2 (velocity as a regression coefficient; docs/02), not a settled number.

## The chain
```
daily_spend  ≈  presence(t)  ×  velocity
presence = Giants home attendance (MLB Stats API, real counts)
anchor   = OI Affinity SF-county daily card-spend index (% deviation vs Jan-2020)
velocity = attendance coefficient in %-space  →  × $/day base  →  dollars
```

## What was built
- `ingest/oi_tracker.py` — OI Affinity SF-county daily spend index (keyless, cached).
- `calibrate/spend_velocity.py` — `calibrate_velocity()` (regress spend on attendance +
  calendar controls) and `dollarize()` (%-lift × explicit $/day base → dollars).

## Result — the calibrated velocity
Regression `spend_idx ~ attendance(/10k) + C(dow) + C(month) + C(year)`, SF county,
2021 + early-2022 (the daily-OI overlap; 875 days, 105 game days):

| quantity | value |
|---|---|
| **lift per 10,000 attendees** | **+1.16 pp of SF-wide daily card spend** |
| 95% CI | [+0.51, +1.82] pp |
| p-value | 0.0005 |
| implied lift at mean attendance (23k) | **+2.7%** |

**More fans → proportionally more citywide spending, and it's statistically strong.**
COVID capacity limits (attendance ranged 3.7k–41k) actually help: they vary the "dose"
and pin down the slope.

## Result — dollarized (ILLUSTRATIVE, biased high)
`dollarize()` needs an absolute SF daily card-spend base (CDTFA's job — not yet wired, so
these use a sensitivity band, clearly provisional):

| SF daily-spend base | event-day lift | $ / attendee |
|---|---|---|
| $75M/day  | ~$2.0M | ~$87 |
| $100M/day | ~$2.7M | ~$116 |
| $125M/day | ~$3.4M | ~$145 |

## Read this honestly
- **The %-lift is real and calibrated; the dollar figures are an order-of-magnitude
  envelope, biased HIGH.** $87–145/attendee sits above the ~$30–70 direct in-market spend
  that event economic-impact studies typically find — the gap is confounding: at
  county-TOTAL grain, game days still coincide with other busy days beyond the day-of-week/
  month/year controls. This is the "spiky target on a smooth aggregate" problem docs/02
  warns about.
- Daily OI ends ~2022-06 → this is a COVID-era estimate; re-check when finer/newer data
  is wired.

## What turns this from an envelope into a trustworthy number
1. **Wire a real CDTFA $ base** (absolute SF food-services taxable sales) — replaces the
   illustrative band; see registry note on the CDTFA data-portal spreadsheet.
2. **Tighten geography / anchor by category** (near-ballpark, dining) to cut confounding.
3. **Scale the panel** — pool across venues/events (docs/02: panel dimension substitutes
   for the short time dimension).
4. **Climb the ladder** — Chow-Lin reconciliation (sum-to-quarterly), then MIDAS, then
   DynamicFactorMQ fusion of BART + Muni + this anchor. (Muni since scrapped — see D12; BART-only.)

## Reproduce
```python
import polars as pl
from eia_pipeline.ingest.oi_tracker import county_daily_spend
from eia_pipeline.calibrate.bart_attendance import fetch_attendance
from eia_pipeline.calibrate.spend_velocity import calibrate_velocity, dollarize
spend = county_daily_spend()                       # SF
att = pl.concat([fetch_attendance(y) for y in (2021, 2022)])
model, summary = calibrate_velocity(spend, att)
dollarize(summary["pp_per_10k"], summary["mean_attendance"], 100_000_000)
```
