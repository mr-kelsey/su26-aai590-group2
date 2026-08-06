# Venue Economics

**Game-day lift forecaster for SF business owners** at **venue-economics.com**. The public
demo of the USD AAI-590 capstone (repo `mr-kelsey/su26-aai590-Group2`; local context at
`~/Projects/personal/capstone/`). Persona (revision 2, 2026-08-04): a business owner picks
THEIR business (typeahead search or a map pin) and a date; the site answers with the
expected lift at their block (+X%, extra visits, ring, game line) plus the citywide ripple
on a light map of the model's 452 grid cells with the project's canonical rings (0-250m /
250-500m / 500m-1km / 1-2.5km / 2.5-5km, the RING_EDGES_M metric standard). No-game dates
say "expect a normal day" and offer the next home games as one-click chips.

**Mode as of 2026-08-06: LIVE, TWO MODELS.** `/api/predict/oracle-ripple` invokes the
SageMaker endpoint `eia-nowcast-oracle-ripple-v1` (Tier 1 LightGBM counterfactual +
canonical-ring DiD effect layer); `/api/predict/oracle-ripple-stgnn` invokes
`eia-nowcast-oracle-ripple-stgnn-v1` (Tier 2 STGNN flow-arm counterfactual grid + its
OWN DiD effect layer, same oracle-ripple/1 wire schema). The UI calls
`/api/predict/compare`, which fans out to every live arm in one request
(Promise.allSettled, asymmetric aborts 20s/10s, secondary live-or-omitted) and drives
the model toggle + compare strip. Handlers in the team repo at
`src/eia_pipeline/serve/`. Full record: **`docs/PLUG-IN-ENDPOINT.md`**. Design spec:
`docs/superpowers/specs/2026-08-04-oracle-ripple-revamp-design.md`.

Two-model honesty rules (do not relax): the STGNN arm is NEVER served simulated (the
arms share one simulator, so a simulated pair would render identical numbers under two
labels); the compare strip suppresses its delta unless both arms are live, same
measure, game date; no MAE appears on the site (the tiers are not scored on a common
basis, docs/PIPELINE.md); one rampMaxPct across arms; Tier 1 is always the default and
carries the neutral `benchmark` chip; a band the effect layer suppressed (bootstrap CI
spans zero, wire `significant: false`) renders as "no detectable effect", never as a
bare +0% (a bare zero inside a positive farther ring reads as a prediction bug and was
reported as one; the STGNN arm ships exactly that shape at 1-2.5km).

Going live needs TWO keys: `status: 'live'` in `config.ts` AND `SAGEMAKER_ENDPOINT_ORACLE`
set in the environment. Vercel binds env vars at build time, so deleting the var (or
setting `ORACLE_FORCE_SIMULATED=1`) takes effect on the NEXT deploy, not the running one;
the immediate rollback lever is Vercel Instant Rollback to the prior production
deployment. `simulate.ts` is kept as that badged fallback and is NOT an automatic one,
because it speaks a different unit at a different magnitude.

**Retired 2026-08-04:** the 540-era county-quarter estimator (county/quarter/attendance form,
`/api/predict`, `county-context.json`, XGBoost endpoint `eia-foodsvc-xgb-v2`). Recover from
git history before commit `60522bc` if ever needed.

## Architecture

- **Model registry** `src/lib/models/`: `types.ts` (envelope + field types), `validate.ts`
  (server-side input validation from field defs), `registry.ts` (id -> handle). Per model:
  `oracle-ripple/config.ts` (input FIELDS + BANDS + rampMaxPct + status), `context.ts`
  (cell geometry, the 400m snap, schedule lookup, band cut: SHARED by the simulator and the
  live adapter so both resolve the user's block and the day's game identically; the
  simulator's own constants deliberately stay out of it), `adapter.ts`
  (buildRequest/parseResponse against the live wire format, with contract assertions that
  throw `EndpointContractError` and become a 502), `simulate.ts` (deterministic preview;
  every constant sourced from the capstone gold tables or labeled heuristic).
- **Two on-demand routes** (`prerender = false`; everything else stays static):
  `src/pages/api/predict/[model].ts` (validate -> simulate while status 'preview'/env
  unset, or SageMaker invoke via adapter when 'live'; 400/404/429/503/500 taxonomy) and
  `src/pages/api/places.ts` (GET `?q=`, business typeahead over the server-side
  Advan-derived index `src/data/pois.json`, 13,966 POIs, top-8 responses only; the full
  index never ships to the client).
- **One React island** `src/components/forecaster/Forecaster.tsx` (client:load) owning
  shared state (place + date live here; the map pin and the form set the SAME place):
  `EventForm.tsx` (config-driven; 'place' kind = debounced typeahead + pin chip),
  `ImpactMap.tsx` (maplibre-gl, click-to-pin, focus-cell outline), `ResultPanel.tsx`
  (focus-first headline, game line, no-game state with next-game chips, band bars, badge).
- **Bundled derivations**: `src/data/giants-schedule.json` (one row per DATE, doubleheaders
  collapsed, day/night + first pitch + opponent + attendance; future games get the
  day/night median attendance, day 36,222 / night 32,898; regenerate
  `scripts/build-schedule.py`) and `src/data/pois.json` (regenerate
  `scripts/build-poi-index.py`). The date field's min/max come from the schedule meta, so
  regenerating the schedule moves the form window automatically.
  **pois.json is NOT committed** (Advan-derived licensed data in a public repo): it lives at
  `s3://aai-590-group2-capstone/website-data/pois.json` and `scripts/fetch-pois.mjs` pulls
  it during the Vercel build (`npm run vercel-build`) with the POIS_* env vars. After
  regenerating it, re-upload: `aws s3 cp src/data/pois.json s3://aai-590-group2-capstone/website-data/pois.json`.
  cells.json and giants-schedule.json ARE committed (aggregated geometry / public MLB facts).
- **Cell geometry** `src/data/cells.json` (committed, ~59 KB): the model's 452 grid cells,
  regenerated by `scripts/build-cells.py`, which mirrors the team pipeline's grid math
  EXACTLY (spatial_units.py: dlat 0.00225 / dlon 0.00284, >= 10 POIs, CITY ILIKE
  'San Francisco', SF bbox; `unit_id = c{gi}_{gj}`). Regenerate (reads the local Advan
  extract, takes ~2 min):
  `cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2/website && /Users/Steve3/Projects/personal/capstone/notebooks/.venv/bin/python scripts/build-cells.py`

## Stack

- **Astro 5 + Tailwind CSS 4** (theme in `src/styles/global.css` `@theme`, NO tailwind
  config file) + TypeScript strict + **React 19** islands + `@astrojs/vercel`.
- **maplibre-gl v6** for the map; basemap is CARTO dark-matter GL style (free with the
  attribution control shown; style URL is one constant in `ImpactMap.tsx`).
- **vitest** unit tests (`npm test`): validation, simulator determinism plus a GOLDEN test
  pinning its exact output (the old determinism test compared simulate() to itself and so
  could not catch a refactor), adapter against REAL recorded endpoint fixtures, and the
  predict route including the live path with the AWS SDK mocked so no test can ever place
  a billed call. 56 tests as of 2026-08-06.
- `@aws-sdk/client-sagemaker-runtime` retained for the live path.

## maplibre v6 + Vite worker gotcha (do not relearn)

maplibre v6 loads its worker via a DYNAMIC `new URL('./maplibre-gl-worker.mjs',
import.meta.url)` that Vite cannot statically analyze. Symptom: map hangs SILENTLY (style
loads, sprite loads, zero tile requests, zero errors, `isStyleLoaded()` forever false) in
dev AND in production builds (no worker chunk emitted). Fix (in `ImpactMap.tsx`): import
the worker with `?worker&url` (Vite bundles it self-contained; it imports
maplibre-gl-shared.mjs so a bare `?url` copy would 404 its import) and call
`setWorkerUrl(maplibreWorkerUrl)`. Dev exposes `window.__map` for debugging.

## Run

- `cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2/website && npm install && npm run dev` -> http://localhost:4321
- `cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2/website && npm test` (vitest)
- `cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2/website && npm run build` (must pass before any push; every push builds a Vercel preview)
- Fresh clones lack `src/data/pois.json` (not committed, see Bundled derivations): either
  copy it from a teammate or run `npm run fetch-data` with the POIS_* env vars set.
- The simulated preview needs NO env vars locally once pois.json is on disk.
  `.claude/launch.json` starts the dev server for browser tooling. The capstone-root
  launch config (`~/Projects/personal/capstone/.claude/launch.json`) starts this same
  dev server LIVE: it injects both SAGEMAKER_ENDPOINT_* names and AWS_REGION via `env`
  (credentials come from the default chain in `~/.aws`).
- Local LIVE testing: a `.env` file is inert for the predict route (`astro dev` never
  copies .env into process.env; only `astro build` does, and the route reads
  process.env). Export in the shell instead: `export AWS_REGION=us-east-2
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... SAGEMAKER_ENDPOINT_ORACLE=eia-nowcast-oracle-ripple-v1 && npm run dev`.

## Env vars (Vercel + local `.env`)

Build-time (set in Vercel, Production + Preview): the POIS_* vars documented in
`.env.example`, used only by `scripts/fetch-pois.mjs` (IAM user `vercel-website-build`,
s3:GetObject on `website-data/*` only). Deliberately NOT the bare AWS_* names.

Runtime (live since 2026-08-06, see `docs/PLUG-IN-ENDPOINT.md`): `AWS_REGION`,
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (long-lived IAM user
`venue-economics-invoke`, one inline `sagemaker:InvokeEndpoint` policy per endpoint
ARN, never temporary session creds), `SAGEMAKER_ENDPOINT_ORACLE`, and
`SAGEMAKER_ENDPOINT_ORACLE_STGNN` (the Tier 2 arm; unset = the arm is omitted from
compare responses and the site renders single-model). The retired
`SAGEMAKER_ENDPOINT` var is deleted from Vercel.

## Simulator provenance (constants in `simulate.ts`)

Distance-decay anchors and per-band totals come from the capstone gold tables
(`event_study_ring` / `distance_decay`, 327 Giants home games 2022-2025, visits measure);
attendance scaling from the gold attendance-tercile slices anchored at tercile-midpoint
attendances (25,729 / 33,112 / 39,544); day/night from the day/night slices. Day/night and
attendance are NOT user inputs: they come from the bundled schedule (actual attendance for
played games, day/night median for future ones). No-game dates return zero lift plus the
next home games. The simulator speaks DAILY VISITS (`measure.id: 'visits'`); the live model
speaks VISITOR-HOURS over hours 16-23. The UI genuinely renders whichever the result
declares now, through `components/forecaster/measureCopy.ts`: it previously hardcoded
"visits" despite this file claiming otherwise. Never mix the two units, and never divide
visitor-hours by roughly four to fake a headcount, because that ratio is a venue-specific
median and not a conversion (capstone lesson).

## Source of truth and deploy flow (cutover DONE 2026-08-04)

- **Source of truth: this repo** `venue-economics/su26-aai590-group2` (GitHub org created
  by mr-kelsey 2026-08-04; the repo transferred there from `mr-kelsey`, old URLs redirect),
  directory `website/`. Make website changes via the team's branch-and-PR flow (protected
  main, one reviewer).
- Vercel builds `website/` from this repo directly: Root Directory `website`, the
  "skip deployments when the root directory is unchanged" toggle ON (replaces the custom
  ignored-build-step command the old runbook prescribed), production branch `main`.
  Merges to `main` deploy production; branch pushes build previews.
- The old deploy mirror `Jungleislander/venue-economics` is retired and archived
  (read-only; history browsable). The sync script it needed is deleted. How the cutover
  went, including the pois.json fetch architecture: **`docs/VERCEL-CUTOVER.md`**.

## Deploy & domain

- **Vercel** project `venue-economics`, team `steves-projects-fdb198c2` (Hobby). Git-connected
  to `venue-economics/su26-aai590-group2` with Root Directory `website`.
- Build command on Vercel is `npm run vercel-build` (auto-preferred over `build`): fetches
  `src/data/pois.json` from S3 (POIS_* env vars), then `astro build`.
- Live: **https://www.venue-economics.com** (www canonical; apex 307s to www; http -> https 308).

## Design (revision 2: Isos design language, light)

- **Source of truth**: `~/Projects/isos/isostech-website/BRANDING.md` + the token file
  `~/Projects/isos/Isos Technology Design System/colors_and_type.css`. This site borrows the
  design LANGUAGE only; no Isos logo, name, or badge appears anywhere.
- **Poppins** self-hosted (OFL TTFs copied from the design system; Light 300 is the display
  weight for the hero headline, body 400, labels 500, buttons 600). Letter-spacing 0.3px on
  display type per the Isos scale. Inter is gone.
- **Light palette** (tokens in `src/styles/global.css` `@theme`): porcelain `#f4f7ff` page,
  white cards (radius 24px, soft cool shadow), mist `#dde7ed` borders, ink `#343941`
  headings, slate `#47535d` / cool-gray `#8b93a5` secondary text, brand red `#b72025`
  (hover `#77161e`) pill CTAs.
- **Map**: CARTO positron (light) basemap; cell choropleth ramp mist `#c7d3da` -> red
  `#77161e` by lift pct; slate rings at the canonical edges 250m/500m/1km/2.5km/5km; red
  venue dot; ink pin with red ring; red outline on the user's focus cell. Crosshair cursor
  invites the click-to-pin.
- The old dark theme (black hero, `ForecastHero.astro` SVG cone, Apple-blue accent) was
  retired 2026-08-04 in revision 2; recover from git history if ever wanted.
- OG image `public/og.png` (1200x630): regenerated 2026-08-04 from the light page (viewport
  screenshot with the Astro dev toolbar removed first).

## Gotchas

- **Git author MUST be `jungleislander@gmail.com`** (repo-local config set). Vercel blocks
  git-push deploys whose commit email has no GitHub account ("Blocked" deployment status).
  Fix forward with a new commit; do NOT amend + force-push anything already pushed.
- `main` is production. Feature work on branches; merge only after review of the branch's
  Vercel preview.
- **Root .gitignore template trap**: the repo-root .gitignore is a Python template whose
  blanket dir rules (`lib/`, `data/`) silently swallowed `website/src/lib/` AND
  `website/src/data/` in PR #9; the first two Vercel builds from this repo died on the
  missing files. The root file now ends with `!website/` + `!website/**`, making
  website/.gitignore the ONLY authority under website/ (pois.json stays ignored there).
  The working tree hides this class of bug, so before pushing website changes verify
  from the git INDEX: `git checkout-index -a --prefix=/tmp/tree/ && cd /tmp/tree/website
  && npm ci && npm run vercel-build && npm test`.
- `scripts/build-cells.py` reads the capstone's local Advan extract
  (`~/Projects/personal/capstone/S3/advan_weekly_patterns/`); it is a dev-machine tool, not
  part of the site build. `cells.json` is committed so builds never touch the data lake.
- The cells are a reconstruction of the team grid from the same source data; when the
  endpoint ships, true-up against Luke's `cell_dim.parquet` (PLUG-IN-ENDPOINT.md step 5).
- **Advan POI names**: many index entries are category placeholders ("Full-Service
  Restaurants") because Advan anonymizes some POIs; that is why search matches addresses
  too and the map pin is the universal fallback. The full index stays server-side (top-8
  responses); mind the Dewey student-license posture if anyone proposes exposing more.
- **Schedule quirk**: doubleheaders are collapsed to one row per DATE in
  `giants-schedule.json` (earliest first pitch, max attendance); the by-date lookup in
  `simulate.ts` depends on date uniqueness.
