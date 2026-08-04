# Oracle Park ripple revamp - design spec

Date: 2026-08-04. Approved by Steve 2026-08-04 (design presented in session; three scoping
decisions answered: retire the old model, simulated preview until the real endpoint lands,
real map with cells plus a band-ring overlay).

## 1. Goal and scope

venue-economics.com becomes the public demo of the USD AAI-590 capstone: an **Oracle Park
game-night ripple forecaster**. A visitor picks a game date, start time, and expected
attendance; the site returns the predicted foot-traffic ripple across San Francisco:

- a real map of the model's ~452 grid cells colored by predicted lift,
- concentric distance-band rings (500m / 1km / 2km / 4km) overlaid,
- a per-band breakdown and a headline number (extra visitor-hours, core-band % lift).

The old county-quarter estimator (540 model) is **retired**: its form, API route, county
data, and build script are deleted. Git history keeps them.

The team has not delivered the model endpoint spec yet (owner: Luke). Until it lands, the
forecaster runs in a clearly-badged **simulated preview** mode. The architecture makes the
eventual plug-in a two-file change (config + adapter) plus env vars, with zero UI work.

Out of scope: dollar figures (calibration not shipped; the response envelope reserves an
optional block), multiple event types (schema-ready, one option for now), moving the repo
between workspaces, merging to main without Steve's sign-off.

## 2. Context: what the endpoint will look like

From the merged team repo (`mr-kelsey/su26-aai590-Group2`, `src/eia_pipeline/nowcast/`):

- The forecaster composes `counterfactual(date, no game) x (1 + band effect)`.
- Tier 1 GBM features: cell statics (`unit_code`, `n_poi`, `food_share`, `dist_venue_m`,
  `bearing_venue_deg`), trailing baselines (`base_k2`, `base_k4`, `base_cap120`,
  `n_cap120`), calendar (`hour`, `dow`, `month`, `t_index`, `us_federal_holiday`),
  weather (`temp_hr`, `prcp_hr`, `wind_hr`), drift (`n_poi_live`).
- Bands: 0-500m, 500m-1km, 1-2km, 2-4km, >4km. Evening window hours 16-23.
- Output: log1p hourly person-hours (presence) per cell.
- Documented forecast core-band effect: +41.4% (train-split estimate, 2023-2024).

User-suppliable inputs reduce to: date, event on/off, start time, attendance, weather
scenario. Everything else is server-derivable or model-internal. The exact request/response
wire format is the unknown the adapter isolates.

## 3. Architecture

```
src/lib/models/
  types.ts               # ModelConfig, InputField, RippleResult, envelope types
  registry.ts            # id -> ModelConfig map
  oracle-ripple/
    config.ts            # id, labels, input fields, band defs, status: 'preview'|'live'
    adapter.ts           # buildRequest()/parseResponse() - TODO markers for the real spec
    simulate.ts          # deterministic mock (section 6)
src/pages/api/predict/[model].ts   # generic route: validate -> live adapter | simulator
src/components/EventForm.tsx       # island: form, fields driven by config
src/components/ImpactMap.tsx       # island: maplibre map + cells + band rings
src/components/ResultPanel.tsx     # headline + band breakdown + preview badge
src/data/cells.json                # generated cell geometry (section 4)
scripts/build-cells.py             # regenerates cells.json from Advan bronze
docs/PLUG-IN-ENDPOINT.md           # the checklist for when the real spec arrives
```

Request flow: form POSTs `{ model, inputs }`-shaped JSON to `/api/predict/oracle-ripple`.
The route looks the model up in the registry, validates inputs server-side against the
config's field definitions (typed coercion, bounds), then:

- if `status === 'live'` AND the model's endpoint env var is set: call
  `adapter.buildRequest()`, invoke SageMaker, `adapter.parseResponse()`;
- otherwise: call `simulate()`.

Response envelope, identical in both modes:

```ts
{
  model: 'oracle-ripple',
  inputs: { ...echoed, normalized },
  result: {
    kind: 'ripple',
    bands: [{ id, label, liftPct, extraVisitorHours }],
    cells: [{ id, liftPct }],          // joins to cells.json client-side
    headline: { extraVisitorHours2km, coreBandLiftPct, window: '16:00-23:00' },
    dollars?: { ... }                  // reserved, absent in v1
  },
  meta: { source: 'simulated' | 'live', version }
}
```

Error taxonomy mirrors the retired route: 400 invalid input, 429 throttled, 503 endpoint
unavailable, 500 with a clean message. No stack traces to the client.

Approach note: this is the "A-lite" option chosen over a fully generic form engine (over-
abstraction for one or two models) and over hard-coding (the current pain). The adapter
boundary is where the unknown lives, so that is where the abstraction goes.

## 4. Map and cell geometry

- **maplibre-gl** is the one new runtime dependency. Basemap: CARTO dark-matter GL style
  (free with attribution, matches the pure-black aesthetic). The style URL lives in one
  constant; swapping to self-hosted PMTiles later is a one-line change.
- The team grid is reconstructible locally: `spatial_units.py` defines cells as a plain
  lat/lon grid, `GRID_DLAT = 0.00225`, `GRID_DLON = 0.00284` (~250m at 37.78N),
  `unit_id = c{gi}_{gj}` with `gi = floor(lat/dlat)`, `gj = floor(lon/dlon)`, keeping
  cells with >= 10 POIs inside bbox lat 37.70-37.84, lon -122.52 to -122.35.
- `scripts/build-cells.py` mirrors that math exactly over the distinct POIs in the local
  Advan bronze (`~/Projects/personal/capstone/S3/advan_weekly_patterns/`), emitting
  `src/data/cells.json`: `{ dlat, dlon, venue: {lat, lon}, cells: [{ id, gi, gj, n_poi,
  food_share, dist_venue_m }] }` (~452 records, small enough to commit). Cell polygons
  derive from gi/gj client-side. The JSON is committed, so site builds never touch the
  data lake.
- Known tolerance: Luke's `poi_dim` may filter POIs slightly differently, so a few edge
  cells may differ from his 452. Ids are grid-derived on both sides, so they are stable;
  when his spec arrives we request `cell_dim.parquet` and true-up (checklist item).
- Band rings at 500m / 1km / 2km / 4km around Oracle Park (37.7786, -122.3893) drawn as
  GeoJSON circles styled in the site's blue glow language, plus a venue marker.
- Map default state (before any prediction): cells at a faint neutral fill, rings visible.
  Predictions re-color the fill by `liftPct`.

## 5. User inputs (v1 form)

| Field | Control | Notes |
|---|---|---|
| Game date | date picker | server derives dow/month features |
| Start time | day / night toggle | maps to first-pitch hour (day ~13:05, night ~18:40) |
| Expected attendance | number | default 38,000; min 1,000; max 42,000 (park capacity ~41.3K) |
| Weather (advanced, collapsed) | temp F, rain toggle | defaults to seasonal-typical for the chosen month |
| Event type | fixed select | one option: Giants home game; more later = config edit |

All bounds, defaults, options, labels come from `oracle-ripple/config.ts`. Adding or
changing fields when the real input list arrives is a config edit; the form component
renders whatever fields the config declares (number / select / toggle / date kinds).

## 6. Simulated preview semantics

Deterministic, no randomness, honest about its source:

- Per-band lift starts from the team's published measured effects: the local gold
  `distance_decay` / `event_study_ring` tables (built from the same data the model trains
  on) and the +41.4% core-band forecast number documented in `nowcast/predict.py`.
  Exact constants are extracted at build time and committed with source comments.
- Attendance scales the effect using the gold event study's attendance-tercile slices
  (piecewise-linear interpolation between tercile multipliers).
- Day/night modulates per the event study's day/night slices.
- Weather nudges the counterfactual baseline, not the lift (rain lowers baseline
  visitor-hours; the game effect is a ratio on top).
- Each cell gets its band's lift shaped by a smooth distance decay within band, so the
  map does not stair-step.
- The UI carries a persistent badge on the result card and map: "Simulated preview:
  computed from the team's measured game effects, not the live model." `meta.source`
  is `simulated`.

## 7. Results panel

- Headline: extra visitor-hours across the evening window (16:00-23:00) within 2km, and
  core-band (0-500m) % lift.
- The map choropleth (section 4).
- Per-band horizontal bars: % lift and absolute extra visitor-hours.
- No dollars in v1; envelope slot reserved.

## 8. Content refresh

- Eyebrow: "Event-impact forecaster". Headline brand moment stays
  ("The economics of live events."). Subhead reframed to the Oracle Park ripple story.
- A short "How it works" section: three sentences (counterfactual trained on non-event
  hours x measured per-band game effects; what simulated preview means; capstone credit,
  USD AAI-590, Farmer / Kelsey / Young).
- Page title, meta description, and OG copy updated. og.png regenerated from the new
  hero if practical in this pass; otherwise a named follow-up.

## 9. Testing

- **vitest** (dev dep): simulator determinism (same inputs, same output; monotone
  attendance scaling; band decay ordering), input validation (bounds, coercion,
  rejection), adapter request-building (activated when the real spec lands).
- `npm run build` must pass. Manual browser smoke test on the dev server: form submit,
  map render, error states, reduced-motion, mobile viewport.

## 10. Rollout

- All work on `feature/oracle-ripple-revamp` in the venue-economics repo. Commit author
  must be `jungleislander@gmail.com` (repo-local config already set; Vercel blocks
  mismatched authors).
- Every push produces a Vercel preview deployment; Steve reviews the preview URL.
- Merge to main (= production deploy) only after Steve's sign-off. Never force-push main.
- After merge: Vercel env vars for the retired endpoint can be removed (harmless if left);
  the new model needs `SAGEMAKER_ENDPOINT_ORACLE` (+ scoped IAM) only when it goes live.

## 11. Plug-in checklist (docs/PLUG-IN-ENDPOINT.md)

When Luke's spec arrives: (1) paste the real input-field list into
`oracle-ripple/config.ts`; (2) implement `buildRequest`/`parseResponse` in `adapter.ts`
against the real wire format; (3) create the scoped IAM user / set
`SAGEMAKER_ENDPOINT_ORACLE` (+ region/account) in Vercel and `.env`; (4) flip
`status: 'live'`; (5) request `cell_dim.parquet` and true-up `cells.json` if cell sets
differ; (6) delete or demote the simulator per team preference (keep as fallback when the
endpoint is down, still badged).

---

# Revision 2 (2026-08-04, same day): Isos light theme + business-owner persona

Requested by Steve after reviewing v1. Three changes; everything else in the v1 spec
(registry architecture, simulated-preview semantics, adapter boundary, testing, rollout)
stands.

## R2.1 Design language: Isos, light, Poppins

- Source of truth: `~/Projects/isos/isostech-website/BRANDING.md` + the Figma-derived
  tokens in `~/Projects/isos/Isos Technology Design System/colors_and_type.css`.
  This borrows the design LANGUAGE (type, palette, surfaces, shapes) for Steve's own
  site; no Isos logo, name, or badge appears.
- Light theme: white surfaces, porcelain `#F4F7FF` page tint, mist `#DDE7ED` borders,
  ink `#343941` headings, slate `#47535D` / cool-gray `#8B93A5` secondary text.
- Brand red `#B72025` (hover `#77161E`) for CTAs and accents; pill buttons
  (radius 100px, weight 600); cards radius 24px with soft cool shadows
  (`0 4px 35px rgba(68,83,94,0.15)`); inputs radius 16px, border `#9a9ca0`-> mist.
- **Poppins throughout, light display weight**: hero headline Poppins 300 (Light) in ink;
  section headings 500/600 per the Isos scale; body 400. Self-hosted TTFs (OFL) copied
  from the design system: Light/Regular/Medium/SemiBold. Inter removed.
- Map: CARTO **positron** (light) GL style replaces dark-matter. Cell lift ramp runs
  mist -> brand red; band rings slate; Oracle Park dot red; user pin ink with red ring.
- The dark ForecastHero SVG backdrop is retired (deleted; git history keeps it).

## R2.2 Persona and inputs: a business owner asking "what lift should I expect?"

Inputs become:

1. **Your business** (field kind `place`): EITHER pick from a typeahead search of the
   model's POIs, OR click the map to drop a pin. Value = `{ lat, lon, name? }`.
   - Search backend: new GET `/api/places?q=` route over a server-side index generated
     from the Advan extract (`scripts/build-poi-index.py` -> `src/data/pois.json`,
     14,467 POIs inside the 452 modeled cells; name + street address + coords). Top 8
     matches per query, matched on name OR address, ranked prefix-first. The index is
     server-side only; clients only ever see the 8 results for their query.
   - Advan caveat: some POIs carry category placeholder names ("Full-Service
     Restaurants") per Advan anonymization; address search and the map-click pin are
     the universal fallback. License note: serving top-8 name/address/coord matches is
     minimal exposure of the licensed extract, flagged to Steve for awareness.
2. **Date** (existing kind): the day they want to know about. Window = the bundled
   Giants home schedule coverage, 2023-01-01 to 2026-09-27 (the last scheduled 2026
   home game in our MLB bronze).

Removed from the form: event type, start time, attendance, weather (all derived or
dropped). Attendance/day-night now come from the schedule:
`scripts/build-schedule.py` -> `src/data/giants-schedule.json` (regular season +
spring/exhibition rows at the park, 2023+; per-date `day_night`, `first_pitch_hour`,
`opponent`, `attendance`). Future games use the day/night median attendance from
played 2023+ games (day 36,222 / night 32,898), labeled `attendanceSource: 'typical'`.

## R2.3 Output: the lift at YOUR spot, on the citywide map

- Resolve the place to its grid cell (`floor(lat/dlat)`, `floor(lon/dlon)`); if that
  cell is not one of the 452 modeled cells, snap to the nearest modeled cell centroid
  within 400m; beyond that, return `focus.outside = true` with a friendly message.
- `result.focus` = `{ cellId, distVenueM, liftPct, extraVisits, bandLabel, outside }`
  for the user's block (the 250m cell containing their pin).
- `result.game` = `{ home: boolean, opponent?, start?, firstPitchHour?, attendance?,
  attendanceSource? }`. **No home game that date** -> lifts are all zero, the result
  says so plainly, and `result.nextGames` lists the next 3 home dates so the user can
  jump to one.
- Headline becomes focus-first: "your block" lift % and extra visits, with the game
  line (opponent, first pitch) under it; citywide within-2km total demotes to a
  secondary stat. Map gains the user's pin and highlights their cell.
- Envelope stays backward compatible (`bands`, `cells`, `headline` unchanged); the
  live adapter contract in `docs/PLUG-IN-ENDPOINT.md` gains the same optional fields.

---

# Revision 3 (2026-08-04, same day): canonical ring edges

Steve flagged that the circle bands did not match the project. The v1/v2 bands
(0-500m / 500m-1km / 1-2km / 2-4km / beyond) came from the nowcast module's
`effects.py`; the PROJECT's canonical rings are the metric standard adopted
2026-07-18 (`RING_EDGES_M` in the team pipeline's `build_silver.py`):
**0-250m / 250-500m / 500m-1km / 1-2.5km / 2.5-5km**. Fixed everywhere: config
bands, map rings (one circle per edge: 250m/500m/1km/2.5km/5km, zoom widened to
fit), result labels ("Core ring (0-250m)", "Citywide within 2.5km",
`headline.extraWithin2p5km` = the four inner rings). Side benefit: the
simulator's per-ring constants are now the gold `event_study_ring` numbers
VERBATIM (18001 / 4146 / 7817 / 37186 / 3762 extra visits; 410.7 / 26.71 /
12.12 / 2.57 / 0.36 pct), no more band aggregation or area scaling. Cells and
pins beyond 5km read zero lift with label "Beyond 5km".
