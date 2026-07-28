# Design — Round 1: pipeline runs in SageMaker Studio, lands gold to S3

*Date: 2026-07-09. Status: approved (brainstorm). Next: implementation plan.*

## Purpose

Get the EIA nowcast pipeline "cleaned up and into Amazon." Concretely: the ingest→land
gold layer runs **inside a SageMaker Studio instance** (using the instance's attached
execution role for AWS access — the boto3-from-laptop path is not reliable here) and
writes the gold Parquet tables to the team S3 bucket, queryable in place.

This is the deployment/infra slice. No modeling (clustering, causal attribution) is in scope.

## Context / constraints that shaped this

- **Access model:** the SageMaker instance role can write to S3 (verified in a prior
  assignment via the notebook/UI); direct API/boto3 from the laptop could not. Therefore
  the pipeline must *run from inside Studio*, not be pushed from local. Matches CLAUDE.md's
  Studio/Lambda-first posture; boto3 remains the call path, just executed in Studio.
- **Repo is already on GitHub** (`github.com/Giant-Leap-ai/eia-nowcast-pipeline`); the
  `task01` branch is pushed. Code reaches Studio via `git clone`.
- **`data/` is gitignored** — the clone carries no data; outputs are (re)generated in Studio.
- **CLAUDE.md:** boto3-first; `ml.m5.large` is quota-constrained (so no Processing job this
  round — prefer notebook + local/Lambda-first); config via env, never hardcode secrets.

## Decisions incorporated (from brainstorm)

| # | Decision |
|---|---|
| D-a | Finish line = code runs in Studio **and** writes gold to S3 (full slice). |
| D-b | Execution model = **Approach C**: run interactively in a notebook now, but drive it through a single reusable entrypoint (`python -m eia_pipeline.run_all --to-s3`) so the same command later drops into a Lambda/Processing job with no rework. |
| D-c | S3 target = **`s3://aai-590-group2-capstone/eia-nowcast/`** (team capstone bucket, our own prefix). |
| D-d | Branch strategy = clean up on `task01`, **PR → merge to `main`**, then clone `main` into Studio. |
| D-e | Muni = **full scrub** (all code, ops/, captured data, docs sections); keep only a D12 decision-log entry. |
| D-f | BART hourly (dead host, D11) = **seed once** to S3 from the existing local artifacts; do not attempt to regenerate this round. |

## Goals (round 1)

1. Repo is clean and merged to `main`: Muni removed, secret exposure addressed.
2. A reusable runner lands the live-reproducible gold tables (CDTFA, OI, MLB) to S3 from Studio.
3. The irreplaceable BART 2024 hourly pull + its derived gold tables are seeded to S3 once.
4. A documented, repeatable Studio runbook, verified by reading a gold table back from S3.

## Non-goals (explicitly deferred)

- SageMaker Processing job / Lambda automation (Approach C builds toward it; doesn't stand
  one up).
- Glue crawler + Athena catalog registration.
- The `bart.py` → capstone Gold-feeds rewrite (the real D11 fix) — fast-follow.
- PeMS ingester; all modeling (clustering, causal attribution).

## Workstream 1 — Cleanup (on `task01`, before merge)

**Muni full scrub.** Delete:
- `src/eia_pipeline/ingest/muni.py`, `src/eia_pipeline/transform/muni_signal.py`,
  `scripts/capture_muni.py`, `tests/test_muni_signal.py`, `ops/` (launchd install/uninstall
  + README), captured `data/raw/muni/*.parquet`.
- Muni sections in docs/04 (Entry 3) and docs/05 (§3.5), plus any Muni rows in source
  ledgers / `config/sources.yaml`.
- Record **D12** in `docs/03_decisions_log.md`: Muni scrapped 2026-07-09 — forward-capture
  only, ~4-week runway can't accumulate enough game-vs-baseline evenings to calibrate.
- **The launchd capture job was never started** (owner confirmed) — nothing to uninstall.
  The 3 files in `data/raw/muni/` are test captures and are deleted in the scrub.

**Secrets.**
- **PeMS:** left as-is per the already-logged team decision (free-tier key, private repo).
  Owner will cancel the key at project submission. Not rotated, not scrubbed this round — do
  not re-litigate the logged decision.
- The 511 token is removed with the Muni docs (Muni scrapped).
- Verify `.env` is gitignored; `.gitignore` currently shows only `data/`. Add `.env` (and
  `.env.*`) if absent — cheap safety against future accidental commits.

**Merge.** PR `task01 → main`; repo owner merges (branch-and-PR convention). Studio clones `main`.

## Workstream 2 — The runner

New `src/eia_pipeline/run_all.py`, entrypoint `python -m eia_pipeline.run_all --to-s3`:

- Calls the **live keyless** ingesters and lands each output through the existing
  `io.land_parquet(df, "gold", "<name>.parquet", to_s3=True)`. Scope = the three anchor
  tables with **no BART dependency** (so they regenerate cleanly from live sources):
  - CDTFA food-services taxable sales (`ingest/cdtfa.py::fetch_food_services`).
  - OI Affinity daily spend index (`ingest/oi_tracker.py::county_daily_spend`).
  - MLB home schedule + attendance (`ingest/mlb.py::fetch_home_schedule`).
- **Not in the runner:** `sf_food_services_daily` (the disaggregated daily $) is
  BART-indicator-derived, so it is **seeded** (Workstream 3), not regenerated. Recomputing
  it live in Studio (read the seeded BART daily-arrivals back from S3, join fresh CDTFA via
  `nowcast/disaggregate.py`) is a documented **fast-follow**, not round 1.
- Idempotent (safe to re-run; overwrites the same keys).
- Logs every S3 URI written and the row count landed.
- `--to-s3` flag; without it, lands local only (dev/dry-run).
- No changes needed to `settings.py` / `io.py` — the S3 plumbing already exists.

## Workstream 3 — Seed the irreplaceable BART artifacts (one-time)

Because the BART hourly host is dead (D11) and `data/` is gitignored, these cannot be
regenerated and are now primary data:
- `data/raw/bart/od_2024.parquet` (the 2024 hourly pull).
- The already-computed BART-derived gold tables: `bart_attendance_calib_2024`,
  `sf_daily_arrivals_2024`, `game_residuals_0_300m`, `sf_food_services_daily_2024`
  (disaggregated daily $ — uses the BART daily-arrivals indicator), and reference tables
  (`bart_stations`, `giants_home_2024`).

Upload once to `s3://aai-590-group2-capstone/eia-nowcast/{raw,gold,reference}/…` via the
S3 console UI (confirmed working) or from within Studio. This is a manual seed step, not
part of the runner.

**Coverage boundary (owner-confirmed acceptable).** Local holdings are **2024 hourly only**
(`od_2024.parquet`: full year, all 24 hours, 8.9M rows). Because it is the raw source,
everything 2024 can be *re-derived* from it, not just the frozen tables — the event study,
the 3.5% attendance calibration, daily-arrivals, and later extensions (MONT, day games).
The **only** thing 2024 hourly cannot support is *multi-year hourly* training (docs/04
Option C assumed OD 2018–2026); those other years' hourly no longer exist anywhere (dead
host). That multi-year model was already slated to run on the capstone **Gold daily-exits
feed** (1998→2025-07, daily grain, in S3, unaffected by the dead host) via the D11 ingester
fix — a fast-follow, not this round. Round 1 is fully covered by the seed.

## S3 layout

```
s3://aai-590-group2-capstone/eia-nowcast/
  raw/       bart/od_2024.parquet, cdtfa/…                (seeded + runner)
  reference/ bart_stations.parquet, giants_home_2024.parquet   (seeded)
  gold/      bart_attendance_calib_2024.parquet           (seeded)
             sf_daily_arrivals_2024.parquet               (seeded)
             game_residuals_0_300m.parquet                (seeded)
             sf_food_services_daily_2024.parquet          (seeded: BART-indicator-derived)
             cdtfa_food_services.parquet                  (runner: live)
             oi_daily_spend.parquet                       (runner: live)
             mlb_home_schedule.parquet                    (runner: live)
```

Config supplied as Studio env vars (not committed): `S3_BUCKET=aai-590-group2-capstone`,
`S3_PREFIX=eia-nowcast`, `AWS_REGION` as appropriate for the instance.

## Studio runbook (the walkthrough)

1. Open the SageMaker Studio instance.
2. `git clone https://github.com/Giant-Leap-ai/eia-nowcast-pipeline.git` (main), `cd` in.
3. `pip install -e .` (Studio images are pip/conda; `uv` optional).
4. Export `S3_BUCKET`, `S3_PREFIX`, `AWS_REGION` in the notebook env.
5. **Write smoke-test:** one-line boto3 `put_object` to `eia-nowcast/_smoketest` → confirms
   the instance role can write the intended prefix before doing real work.
6. Seed the BART artifacts (Workstream 3) if not already uploaded.
7. `python -m eia_pipeline.run_all --to-s3`.
8. Confirm objects landed (Verification below).

## Verification

- `io.list_s3("eia-nowcast/")` lists the expected keys.
- **Read-back in place:** DuckDB `SELECT count(*)` over one gold table read directly from
  its `s3://…` URI, asserting a non-zero / expected row count — proves the data is readable
  where it landed, not merely uploaded.
- The write smoke-test (runbook step 5) gates the whole run: if it fails, stop and diagnose
  role permissions before anything else.

## Risks / open items

- **Studio role S3 scope unverified for writes** — mitigated by the step-5 smoke-test.
- **PeMS rotation depends on the repo owner** — out of our hands; we flag and scrub the file.
- **`pip install -e .` in the Studio base image** may need a specific Python/kernel; confirm
  3.11 or adjust. Fallback: install deps explicitly.
- **Seed upload path** assumes the S3 console UI works for the target prefix (confirmed for
  this bucket in a prior assignment); if not, seed from within Studio instead.
