# 03 — Decisions Log

Short, dated decisions with rationale. **Do not silently reverse these.** If you
believe one is wrong, argue it in a PR description.

### D1 — Nowcasting framing is the spine, not a workaround
Daily dollars are inferred (presence × calibrated velocity), validated only at the
anchor grain, reported with intervals. Framed as mixed-frequency nowcasting /
temporal disaggregation. *Rationale: no free high-frequency dollar feed exists;
inferring it is the contribution.*

### D2 — Advan foot traffic via Dewey is OUT; demoted to nice-to-have
Dewey access unavailable. Advan is the SafeGraph-patterns successor, and its academic
channel was Dewey, so the granular CBG path is closed to us for free. Alternatives
(Spectus/StreetLight/Placer/Veraset) are gated/paid; none free+instant.
*Consequence:* on-site presence leans on transit + wastewater + (limited) satellite;
the CBG-grain ring/distance-decay target is reshaped accordingly.

### D3 — Satellite parking is narrow-scope only
Free imagery (Sentinel-2, 10m) **cannot count individual cars** (needs sub-meter).
Free tier gives a **lot-fill index** only. Fixed ~10:30 local overpass + ~5-day
revisit + clouds ⇒ works only for **isolated-lot, daytime, recurring** venues
(e.g., Oakland Coliseum). Useless for dense downtown venues (Oracle Park, Chase Center)
and evening events. *Rationale: physics of GSD + overpass timing.*

### D4 — Freeway loops (PeMS) are a regional covariate, not a venue signal
PeMS **flow** = vehicle counts on freeways. Clean where a venue is hung off one ramp
with big lots (Coliseum/I-880). For dense downtown SF venues the nearest loops carry
all city-bound traffic → cannot isolate event arrivals. PeMS is freeway-only; no
surface-street equivalent. *Consequence:* for SF venues, **transit (BART + Muni) is
the primary inflow proxy** — the density that kills parking makes transit dominant.

### D5 — Wastewater: use flow-residual (crude), not pathogen dilution (elegant)
Mass-load normalization (marker concentration × flow, dilution-invariant) is the
principled method but needs paired PMMoV + flow at usable cadence, which public data
(twice-weekly) doesn't provide. **Operational path:** regress daily flow on
precipitation + antecedent soil moisture + ET + temperature; treat the residual as
the candidate crowd anomaly. Model as dynamic regression w/ distributed-lag event
term + ARIMA errors; hierarchical panel across facilities for partial pooling.
*Caveats:* weather dominates flow; daily is the public granularity floor; best in
small sewersheds; signal = **net imported visitors**, not gross attendance.

### D6 — Dollar anchors are multi-category, and lodging is the strongest
CDTFA C08 (food, county-quarter) is the calibration anchor, not the primary target.
TOT + AirDNA give near-direct **lodging** dollars and are the strongest/highest-freq
dollar signal, especially in Tier-3 towns. OI tracker covers retail/entertainment
at county-week. *Rationale: C08 alone only anchors food-services velocity.*

### D7 — Direct vs. induced boundary is firm
This pipeline estimates **direct visitor spend**. Multipliers (BEA/RIMS II/IMPLAN)
expand to indirect + induced as a separate layer. Never let multipliers touch calibration.

### D8 — Scope stays generalizable (multi-county, multi-event-type)
No narrowing to a single venue/event type. Generalization proven via leave-one-county-
out and leave-one-event-type-out CV across a stratified corpus. The panel dimension
also makes the mixed-frequency estimation feasible despite short per-county series.

### D9 — Licensing discipline
Sports-Reference is off-limits (ToS bars AI-training use); use direct league APIs.
No scraping ToS-fraught live-availability sources. Avoid location-broker data under
FTC action (X-Mode/Outlogic) or breached/defunct (Gravy/Unacast, Near, Tamoco).
Prefer the government/open tier as the backbone.

### D10 — Advan foot traffic is back IN (reverses D2)
D2 demoted Advan to nice-to-have because the free Dewey academic channel was closed.
That constraint no longer holds: we now have **licensed Advan Weekly Patterns** in the
team capstone bucket (`s3://aai-590-group2-capstone/Gold/advan_weekly_patterns/`,
2020-01→2026-05, per-POI weekly with hourly-within-week + visitor-origin CBGs). Advan
is now the **primary on-site presence layer** — venue-grain visits at Oracle Park and
its 1 km restaurant ring, replacing the ~3.5% BART-through-EMBR proxy for the on-site
slice. *Rationale: the blocker in D2 was access, not fit; access is resolved.* D2's
physics/ToS notes about the paid alternatives still stand; only the Advan verdict flips.
Licensing: Advan is licensed data via the capstone subscription (not scraped, not a
location-broker under FTC action) — consistent with D9.

### D11 — BART hourly-OD host is dead; pivot feeds
The hourly OD host our `ingest/bart.py` used (`afcweb.bart.gov` / `64.111.127.166`,
`date-hour-soo-dest-YYYY.csv.gz`) went dead ~2026-07-02; DataSF daily exits went stale
2025-07-31. No public hourly successor exists. **Pivot:** daily-exits CSV (1998→2025-07,
Gold `bart_daily_exits/`) for the daily game-day contrast + monthly OD workbooks (Gold
`bart_monthly_od/`) for post-2025-07 at monthly grain. Our landed 2024 hourly pull
remains valid history. *Consequence:* fix `ingest/bart.py` to read the Gold feeds.

### D12 — Muni front-door signal is scrapped
Muni (511 SIRI occupancy at the Oracle Park gate) was forward-capture only — no historical
Muni stop-hour data exists anywhere (see the prior negative finding). With ~4 weeks to the
capstone deadline (~2026-08-06), the capture window cannot accumulate enough game-vs-baseline
evenings to calibrate a defensible front-door coefficient. Scrapped 2026-07-09: code,
capture job, ops/, captured data, and docs removed. BART (EMBR, ~3.5% capture) remains the
working presence signal. *Rationale: insufficient forward-capture window, not a data-quality
problem. Reversible in principle, but not worth the runway now.*
