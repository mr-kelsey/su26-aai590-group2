# 00 — Project Overview

## The problem
Incumbent economic-impact tools (IMPLAN, RIMS II) are expensive and slow. We are
building a data-driven pipeline that estimates the economic impact of social events
(pro/college sports, concerts, races, festivals) county-by-county across California,
trained on public and commercial **proxy signals** and validated against official
tax data.

## The economic decomposition
Total impact = **direct** + **indirect** + **induced**.

- This pipeline estimates **direct visitor spend**. That is the hard, novel part.
- Indirect + induced are handled by a separate multiplier layer (BEA/RIMS II/IMPLAN)
  that takes our direct estimate as input. **Keep these separate.** Do not let the
  multiplier layer touch the calibration, or you double-count.

Direct spend itself decomposes as:

    spend = (# people) × (share who spend) × (dollars per spender) × (local capture)

or, in the form this pipeline uses:

    spend_rate(t) = presence(t) × spending_velocity(segment, category, place)

## Why "nowcasting"
The only thing denominated in **real dollars** is slow: CDTFA taxable sales are
county-quarter and lag 6 months to 2+ years; TOT is annual (centralized) or monthly
(per-city). Meanwhile presence proxies are hourly-to-daily. Producing a daily dollar
estimate from a quarterly dollar truth + daily indicators is exactly the
**mixed-frequency nowcasting / temporal disaggregation** problem that central banks
solve to estimate GDP before it is published. Framing our task this way is the
methodological contribution, not a workaround. See `02_modeling_and_nowcasting.md`.

## What "presence" we can actually get (summary; full detail in 01)
- **Transit:** BART hourly origin-destination exits (excellent, Bay Area only).
- **Freeway:** Caltrans PeMS loop-detector **flow** (vehicle counts), District 4
  (Bay Area) and District 7 (LA). Regional inflow, not last-mile.
- **Wastewater:** treatment-plant influent/effluent flow (eSMR), weather-adjusted
  residual → net imported visitors. Best in small sewersheds (Tier-3 towns).
- **Satellite:** lot-fill index from Sentinel-2 (free) — only for isolated-lot,
  daytime, recurring venues. Cannot count individual cars on free imagery.
- **Anticipation:** Wikipedia pageviews, ticket pricing (SeatGeek/Ticketmaster).
- **Lodging (near-dollar):** AirDNA revenue (daily), TOT (monthly/annual).

## The identification insight (carried from AAI-540)
Event lift is only identifiable **within** a place, not cross-sectionally — between-
county level variation swamps it. Aggregate proxies (flow, transit, spend) only move
for **net-imported** units (out-of-area visitors), not locals relocating within the
area. That is a feature: visitors are precisely the imported money we want. It also
means the sensitive test cells are **Tier-3** cities (Indio, Napa, Paso Robles,
Monterey…) where a single large event materially moves a quarterly/monthly figure.
