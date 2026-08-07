# CLAUDE.md — read this first, every session

## Mission
Build a data pipeline that estimates the **daily economic impact of social events**
(sports, concerts, races, festivals) across California counties, by fusing many
high-frequency *presence* proxies against slow, trustworthy *dollar* anchors.
The end goal is a defensible **daily dollar estimate** at event grain — something
no source measures directly, so we **nowcast** it.

## The one idea that explains the whole repo
We cannot *observe* daily spend. We *infer* it:

    daily_spend ≈ presence(t)  ×  spending_velocity(segment, category, place)

- **presence(t)** comes from cheap, fast signals (BART exits, PeMS freeway flow,
  wastewater flow residual, foot traffic where available). Hourly-to-daily.
- **spending_velocity** is NOT measured — it is a *calibrated coefficient*, fit
  against slow dollar anchors (CDTFA taxable sales, the Opportunity Insights
  spend tracker, Transient Occupancy Tax) and then applied back down at event grain.

Everything in `src/` exists to (1) ingest a presence or anchor signal, (2) land it
in a common panel, (3) calibrate velocity against an anchor, (4) nowcast daily spend.
Read `docs/02_modeling_and_nowcasting.md` before touching `calibrate/` or `nowcast/`.

## How to work in this repo
- **Python 3.11**, environment managed with **`uv`**. `uv sync` then `uv run ...`.
- **Local EDA:** DuckDB + Polars. Pull a source, land Parquet, query with DuckDB.
- **AWS calls:** use **boto3 as the primary call path** (it is the reliable one here).
  If using the SageMaker SDK, pin **`sagemaker<3`** — v3 restructured the package
  and breaks imports in the baked images. Import from submodules, not top-level
  re-exports (e.g. `from sagemaker.session import Session`).
- **Compute:** `ml.m5.large` is quota-constrained. Prefer **Lambda-first** workers
  and local processing over standing up training instances for ingestion/ETL.
- **Git:** branch-and-PR, small and reviewable. `main` is protected and every PR
  needs **at least one reviewer** to sign off. Branch names are `feature/`,
  `bugfix/`, `hotfix/`, `release/`, or `docs/` + lowercase-hyphenated words
  (no underscores). See the root `README.md` for the full team convention.
- **Config, never hardcode:** all environment-specific coordinates (AWS account,
  bucket, prefix, region) come from env via `src/eia_pipeline/settings.py`.
  See `.env.example`. **Never commit real values or secrets.**

## Guardrails (please respect — these are settled)
- **Do not re-litigate decisions in `docs/03_decisions_log.md`.** They were made
  deliberately with reasons. If you think one is wrong, flag it in a PR description;
  do not silently reverse it.
- **Licensing discipline.** Only ingest sources marked build-now/keyed in
  `config/sources.yaml`. Do **not** scrape ToS-restricted sources (live OpenTable/
  Resy availability, Sports-Reference, location-broker data). If a source isn't in
  the registry, add it to the registry (with license + status) before writing an ingester.
- **Secrets are pointers, not values.** Keys live in AWS Secrets Manager or a
  gitignored local `.env`. Never paste a key into code, a commit, or chat.
- **Presence ≠ people ≠ dollars.** Every proxy needs a conversion factor
  (persons/vehicle, mode share, gallons/person, dollars/person-hour). These factors
  are *calibrated*, never assumed. Keep them explicit and in `calibrate/`.

## Where to start
`tasks/01_first_task.md` — a fully-specified, **zero-credential** end-to-end slice
(Open-Meteo weather + CA eSMR wastewater flow for one facility + one known event).
It proves the ingest → land → query → residual loop before we scale to the constellation.
`tasks/backlog.md` has the sequence after that.

## Repo map
- `docs/`        — mission, data strategy, modeling framework, decisions log
- `config/`      — `sources.yaml` machine-readable data-source registry
- `schemas/`     — target panel schema + venue↔geography crosswalk (the join glue)
- `tasks/`       — first task + backlog
- `src/eia_pipeline/`
  - `settings.py`  — env-driven config (no secrets inline)
  - `io.py`        — land/read Parquet, local + S3 helpers
  - `ingest/`      — one module per source (stubs; build incrementally)
  - `transform/`   — residualization, joins, panel assembly
  - `calibrate/`   — velocity calibration against dollar anchors
  - `nowcast/`     — temporal disaggregation → MIDAS → DFM → state-space/BSTS
  - `serve/`       : turns the Tier 1 GBM into something the website can call:
    panel rebuild, 2026 covariate extension, canonical-ring effect layer
    (DiD on calendar-matched controls, month x weekend strata: the unmatched
    pool manufactured a hollow ring on the demo map, see effects_v2's module
    docstring and PR #20), `model.tar.gz`, SageMaker deploy. **It never modifies `nowcast/` or the
    training window.** `model_hour.parquet` and `rolling_baseline.parquet` are
    never rewritten; the serve path builds parallel `*_serve` tables and
    `spine_2026.verify()` proves the overlap is bit-identical. Two things in here
    are load-bearing and easy to break silently: `featurespec.py` is the ONE
    feature-matrix builder, copied verbatim into the tarball and hash-checked at
    container start (passing a plain numpy array instead scores every cell as its
    lexicographic neighbour, with no exception); and the control CTE in
    `build_rolling_baseline_serve` filters `clean_control_strict AND observed`,
    without which every unobserved future zero flattens the baselines after it.
    `tests/test_serve_invariants.py` pins both. `serve/stgnn.py` is the Tier 2 arm behind the SAME wire contract: flow-arm STGNN checkpoint, 24-hour `*_serve_24` panel (bit-identical to training on the overlap), t_index clamped at the panel end, counterfactual grid precomputed into the tarball (no torch in the container), and its own effects_stgnn.json from STGNN residuals through the injectable effects_v2 estimator. Endpoint `eia-nowcast-oracle-ripple-stgnn-v1`; every deploy/package/smoke command takes `--model oracle-ripple-stgnn`.

### Also in this repo (team-side, imported 2026-07-28)
- `pipeline/`    — the team's DuckDB medallion build: `build_silver.py`
  (ring×day panel) and `build_gold.py` (game effects, event study, distance
  decay). Bare `pip install -r pipeline/requirements.txt`; **not** on `uv`.
- `notebooks/`   — Advan hourly exploration.
- `README_eia_nowcast.md` — this sub-project's own README (root `README.md` is
  the team's).

Two things are deliberately **not** reconciled yet, so don't "fix" them silently:
1. **Rings differ.** `pipeline/` uses metric edges (0-250m, …); `src/eia_pipeline/`
   uses 0-300m. The two are cross-checked, not identical (corr 0.9965).
2. **Two dependency systems** coexist (`pyproject.toml`/`uv.lock` vs
   `pipeline/requirements.txt`). Convergence is a separate, reviewed change.

`pipeline/build_silver.py` and `build_gold.py` read this sub-project's S3 output
at `s3://<bucket>/eia-nowcast/gold/game_residuals_0_300m.parquet`. **Moving that
prefix breaks their build** — treat the path as a contract.

## Working style
Build **incrementally**. Run a step, show real output, proceed. Do not emit a large
pre-solved pipeline in one shot — each source and each modeling step should be small
enough to inspect and understand. Prefer honest caveats over confident hand-waving.
