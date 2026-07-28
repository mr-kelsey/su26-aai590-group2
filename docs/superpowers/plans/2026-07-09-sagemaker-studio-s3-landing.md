# SageMaker Studio → S3 Gold Landing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up the repo (scrap Muni, fix `.gitignore`), add a reusable runner that lands the live anchor gold tables to S3 from inside SageMaker Studio, and document a verified Studio runbook — round 1 of "into Amazon."

**Architecture:** A single thin entrypoint `python -m eia_pipeline.run_all --to-s3` calls the three BART-independent live ingesters (CDTFA, OI, MLB) and lands each via the existing `io.land_parquet(..., to_s3=True)` to `s3://aai-590-group2-capstone/eia-nowcast/gold/`. BART-derived artifacts (dead host, D11) are seeded to S3 once, manually. The pipeline runs inside Studio using the instance's execution role — not boto3 from a laptop.

**Tech Stack:** Python 3.11, `uv`, Polars, DuckDB, boto3, pytest. Existing modules: `eia_pipeline.io`, `eia_pipeline.settings`, `eia_pipeline.ingest.{cdtfa,oi_tracker,mlb}`.

## Global Constraints

- Python 3.11; environment via `uv` (`uv sync`, `uv run ...`).
- boto3 is the AWS call path; run from **inside Studio** (instance role), not laptop API.
- `ml.m5.large` is quota-constrained → **no Processing job / training instance** this round.
- Config from env only; **never hardcode** AWS coordinates or secrets. S3 target is
  `S3_BUCKET=aai-590-group2-capstone`, `S3_PREFIX=eia-nowcast`.
- Do **not** re-litigate `docs/03_decisions_log.md`; the Muni scrap is added as **D12**.
- PeMS creds: leave as-is (logged team decision); do not rotate or scrub this round.
- Small, reviewable commits; TDD where there is testable logic.

---

### Task 1: Scrap Muni (code, tests, ops, data, docs)

**Files:**
- Delete: `src/eia_pipeline/ingest/muni.py`, `src/eia_pipeline/transform/muni_signal.py`,
  `scripts/capture_muni.py`, `tests/test_muni_signal.py`, `ops/` (whole dir),
  `data/raw/muni/` (whole dir, gitignored — remove from disk).
- Modify: `docs/04_data_exploration.md` (remove Muni Entry 3 + source-ledger row),
  `docs/05_data_access_guide.md` (remove §3.5 Muni + the `FIVEONEONE_TOKEN` row in §2),
  `config/sources.yaml` (remove any Muni/511 entry).

**Interfaces:**
- Consumes: nothing.
- Produces: a repo with no Muni references (verified by grep in Step 3).

- [ ] **Step 1: Delete the Muni code, tests, ops, and captured data**

```bash
git rm src/eia_pipeline/ingest/muni.py \
       src/eia_pipeline/transform/muni_signal.py \
       scripts/capture_muni.py \
       tests/test_muni_signal.py
git rm -r ops
rm -rf data/raw/muni          # gitignored — just remove from disk
```

- [ ] **Step 2: Remove Muni from docs and the source registry**

Edit `docs/04_data_exploration.md`: delete the `### 3. Muni front door …` subsection and any Muni row in the "Source ledger" table.
Edit `docs/05_data_access_guide.md`: delete `### 3.5 511.org SIRI StopMonitoring …` and the `FIVEONEONE_TOKEN` row in the §2 credentials table.
Edit `config/sources.yaml`: delete the Muni / 511 source entry if present (grep to confirm the key name first: `grep -niE 'muni|511|fiveoneone|siri' config/sources.yaml`).

- [ ] **Step 3: Verify no Muni references remain anywhere in tracked source/docs/config**

Run: `grep -rniE 'muni|511|fiveoneone|siri|stopmonitoring' src/ tests/ scripts/ docs/ config/ 2>/dev/null | grep -v '03_decisions_log'`
Expected: **no output** (empty). (The D12 entry added in Task 2 is the only allowed mention.)

- [ ] **Step 4: Verify the package still imports and the test suite still collects**

Run: `uv run python -c "import eia_pipeline.io, eia_pipeline.settings, eia_pipeline.ingest.cdtfa, eia_pipeline.ingest.oi_tracker, eia_pipeline.ingest.mlb; print('imports OK')"`
Expected: `imports OK` (no ModuleNotFoundError from a dangling Muni import).
Run: `uv run pytest -q`
Expected: passes; `test_muni_signal.py` is gone and no collection error references Muni.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Scrap Muni: remove ingester, transform, capture job, ops, docs (D12 to follow)"
```

---

### Task 2: Record D12 (Muni scrap) in the decisions log

**Files:**
- Modify: `docs/03_decisions_log.md` (append after the last decision, currently D11).

**Interfaces:**
- Consumes: nothing.
- Produces: a permanent record of the scrap so it is not re-litigated.

- [ ] **Step 1: Append the D12 entry**

Add to `docs/03_decisions_log.md`, following the existing `### Dn — …` heading style:

```markdown
### D12 — Muni front-door signal is scrapped
Muni (511 SIRI occupancy at the Oracle Park gate) was forward-capture only — no historical
Muni stop-hour data exists anywhere (see the prior negative finding). With ~4 weeks to the
capstone deadline (~2026-08-06), the capture window cannot accumulate enough game-vs-baseline
evenings to calibrate a defensible front-door coefficient. Scrapped 2026-07-09: code,
capture job, ops/, captured data, and docs removed. BART (EMBR, ~3.5% capture) remains the
working presence signal. *Rationale: insufficient forward-capture window, not a data-quality
problem. Reversible in principle, but not worth the runway now.*
```

- [ ] **Step 2: Verify the entry is present and well-formed**

Run: `grep -n '### D12' docs/03_decisions_log.md`
Expected: one match printing the D12 heading line.

- [ ] **Step 3: Commit**

```bash
git add docs/03_decisions_log.md
git commit -m "Log D12: Muni front-door signal scrapped (deadline-driven)"
```

---

### Task 3: Add `.env` to `.gitignore`

**Files:**
- Modify: `.gitignore` (currently: `data/`, `.venv/`, `__pycache__/`, `*.pyc`, `.DS_Store`).

**Interfaces:**
- Consumes: nothing.
- Produces: `.env` and `.env.*` are ignored (safety against future secret commits).

- [ ] **Step 1: Append the ignore rules**

Add these two lines to the end of `.gitignore`:

```
.env
.env.*
```

- [ ] **Step 2: Verify `.env` would be ignored**

Run: `git check-ignore -v .env`
Expected: a line showing `.gitignore:N:.env	.env` (a match). If `.env` does not exist on disk, this still resolves the rule; create an empty one to confirm if needed: `touch .env && git check-ignore -v .env && rm .env`.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "gitignore .env and .env.* (secret-safety)"
```

---

### Task 4: The runner module `run_all.py` — CDTFA table (TDD, local-only)

**Files:**
- Create: `src/eia_pipeline/run_all.py`
- Create: `tests/test_run_all.py`

**Interfaces:**
- Consumes: `eia_pipeline.io.land_parquet(df, *parts, to_s3=False) -> str`;
  `eia_pipeline.ingest.cdtfa.fetch_food_services(county="SAN FRANCISCO") -> pl.DataFrame`.
- Produces: `run_all.land_table(name: str, df: pl.DataFrame, to_s3: bool) -> str` — writes
  `gold/<name>.parquet` (local, and S3 when `to_s3=True`), returns the written path/URI.
  Later tasks (5, 6, 7) call `land_table` for OI, MLB, and the CLI.

- [ ] **Step 1: Write the failing test for `land_table` (no network — synthetic frame)**

```python
# tests/test_run_all.py
import types

import polars as pl
from eia_pipeline import run_all


def test_land_table_writes_gold_parquet_locally(tmp_path, monkeypatch):
    # `settings` is a frozen dataclass — don't mutate its attrs. Swap the whole `settings`
    # object that `io` references for a lightweight stand-in pointing data_dir at tmp_path.
    from eia_pipeline import io as io_mod
    monkeypatch.setattr(io_mod, "settings", types.SimpleNamespace(data_dir=tmp_path))

    df = pl.DataFrame({"county": ["SAN FRANCISCO"], "value": [123]})
    path = run_all.land_table("cdtfa_food_services", df, to_s3=False)

    written = tmp_path / "gold" / "cdtfa_food_services.parquet"
    assert written.exists()
    assert str(written) == path
    assert pl.read_parquet(written).equals(df)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_all.py::test_land_table_writes_gold_parquet_locally -v`
Expected: FAIL — `ModuleNotFoundError: eia_pipeline.run_all` (module not created yet).

- [ ] **Step 3: Create `run_all.py` with `land_table` and the CDTFA builder**

```python
# src/eia_pipeline/run_all.py
"""Round-1 runner: land the BART-independent live anchor gold tables to S3.

Run inside SageMaker Studio (instance role provides S3 access):
    python -m eia_pipeline.run_all --to-s3        # land to local + S3
    python -m eia_pipeline.run_all                 # local only (dev/dry-run)

BART-derived tables are seeded to S3 manually (dead host, D11) — not built here.
"""
from __future__ import annotations

import argparse

import polars as pl

from . import io
from .ingest import cdtfa, mlb, oi_tracker


def land_table(name: str, df: pl.DataFrame, to_s3: bool) -> str:
    """Write `df` to gold/<name>.parquet (local always; S3 when to_s3). Returns path/URI."""
    return io.land_parquet(df, "gold", f"{name}.parquet", to_s3=to_s3)


def build_cdtfa() -> pl.DataFrame:
    """SF food-services (C08) quarterly taxable sales — the dollar anchor."""
    return cdtfa.fetch_food_services()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_all.py::test_land_table_writes_gold_parquet_locally -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eia_pipeline/run_all.py tests/test_run_all.py
git commit -m "runner: land_table helper + CDTFA builder (TDD)"
```

---

### Task 5: Add OI and MLB builders to the runner

**Files:**
- Modify: `src/eia_pipeline/run_all.py`
- Modify: `tests/test_run_all.py`

**Interfaces:**
- Consumes: `oi_tracker.county_daily_spend() -> pl.DataFrame`;
  `mlb.fetch_home_schedule(season: int) -> pl.DataFrame`.
- Produces: `build_oi() -> pl.DataFrame`, `build_mlb(seasons: list[int]) -> pl.DataFrame`.
  `build_mlb` concatenates one `fetch_home_schedule` per season.

- [ ] **Step 1: Write the failing test for `build_mlb` season concatenation (monkeypatched, no network)**

Add to `tests/test_run_all.py`:

```python
def test_build_mlb_concats_seasons(monkeypatch):
    from eia_pipeline import run_all

    def fake_schedule(season):
        return pl.DataFrame({"season": [season], "game_pk": [season * 10]})

    monkeypatch.setattr(run_all.mlb, "fetch_home_schedule", fake_schedule)
    out = run_all.build_mlb([2023, 2024])
    assert out.height == 2
    assert sorted(out["season"].to_list()) == [2023, 2024]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_all.py::test_build_mlb_concats_seasons -v`
Expected: FAIL — `AttributeError: module 'eia_pipeline.run_all' has no attribute 'build_mlb'`.

- [ ] **Step 3: Add the OI and MLB builders**

Append to `src/eia_pipeline/run_all.py`:

```python
def build_oi() -> pl.DataFrame:
    """OI Affinity SF-county daily card-spend index (%-shape anchor)."""
    return oi_tracker.county_daily_spend()


def build_mlb(seasons: list[int]) -> pl.DataFrame:
    """MLB Giants home schedule + attendance, one row per game, across seasons."""
    return pl.concat([mlb.fetch_home_schedule(s) for s in seasons], how="vertical")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_all.py::test_build_mlb_concats_seasons -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/eia_pipeline/run_all.py tests/test_run_all.py
git commit -m "runner: OI and MLB builders"
```

---

### Task 6: Wire the CLI (`main` + `__main__`) with a `--to-s3` flag

**Files:**
- Modify: `src/eia_pipeline/run_all.py`

**Interfaces:**
- Consumes: `build_cdtfa`, `build_oi`, `build_mlb`, `land_table` (Tasks 4–5).
- Produces: `main(argv: list[str] | None = None) -> list[str]` — builds all three tables,
  lands each, returns the list of written paths/URIs; `python -m eia_pipeline.run_all`
  entrypoint. `--to-s3` toggles S3 landing; `--seasons` sets MLB seasons (default `[2024]`).

- [ ] **Step 1: Write the failing test for `main` (monkeypatch builders + land_table; no network, no S3)**

Add to `tests/test_run_all.py`:

```python
def test_main_lands_three_tables(monkeypatch):
    from eia_pipeline import run_all

    landed = []
    monkeypatch.setattr(run_all, "build_cdtfa", lambda: pl.DataFrame({"a": [1]}))
    monkeypatch.setattr(run_all, "build_oi", lambda: pl.DataFrame({"b": [2]}))
    monkeypatch.setattr(run_all, "build_mlb", lambda seasons: pl.DataFrame({"c": [3]}))

    def fake_land(name, df, to_s3):
        landed.append((name, to_s3))
        return f"path/{name}"

    monkeypatch.setattr(run_all, "land_table", fake_land)

    out = run_all.main(["--seasons", "2024"])   # no --to-s3 => local only
    assert [n for n, _ in landed] == ["cdtfa_food_services", "oi_daily_spend", "mlb_home_schedule"]
    assert all(to_s3 is False for _, to_s3 in landed)
    assert len(out) == 3
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_all.py::test_main_lands_three_tables -v`
Expected: FAIL — `AttributeError: ... has no attribute 'main'`.

- [ ] **Step 3: Implement `main`, arg parsing, and the `__main__` guard**

Append to `src/eia_pipeline/run_all.py`:

```python
_TABLES = [
    ("cdtfa_food_services", lambda seasons: build_cdtfa()),
    ("oi_daily_spend", lambda seasons: build_oi()),
    ("mlb_home_schedule", lambda seasons: build_mlb(seasons)),
]


def main(argv: list[str] | None = None) -> list[str]:
    ap = argparse.ArgumentParser(description="Land BART-independent gold tables to S3.")
    ap.add_argument("--to-s3", action="store_true", help="also upload to S3 (needs S3_BUCKET)")
    ap.add_argument("--seasons", nargs="+", type=int, default=[2024], help="MLB seasons")
    args = ap.parse_args(argv)

    written: list[str] = []
    for name, builder in _TABLES:
        df = builder(args.seasons)
        dest = land_table(name, df, to_s3=args.to_s3)
        print(f"landed {name}: {df.height} rows -> {dest}")
        written.append(dest)
    return written


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run the full runner test file to verify all pass**

Run: `uv run pytest tests/test_run_all.py -v`
Expected: all three tests PASS.

- [ ] **Step 5: Local smoke run (real network, local-only — proves the builders actually fetch)**

Run: `uv run python -m eia_pipeline.run_all --seasons 2024`
Expected: three `landed …` lines with non-zero row counts; files appear under `data/gold/`. (This hits live CDTFA/OI/MLB; if a source is briefly down, note it and re-run — do not mock it away.)

- [ ] **Step 6: Commit**

```bash
git add src/eia_pipeline/run_all.py tests/test_run_all.py
git commit -m "runner: CLI main + --to-s3 / --seasons; local smoke verified"
```

---

### Task 7: Studio runbook + seed manifest in docs

**Files:**
- Create: `docs/06_aws_studio_runbook.md`

**Interfaces:**
- Consumes: the runner CLI (Task 6); the seed list (spec Workstream 3).
- Produces: a copy-pasteable runbook a teammate can follow in Studio; the authoritative
  list of which local files to seed to which S3 keys.

- [ ] **Step 1: Write the runbook**

Create `docs/06_aws_studio_runbook.md` with these sections (fill with the exact commands):

```markdown
# 06 — AWS SageMaker Studio Runbook (round 1: land gold to S3)

Runs the gold layer from inside a Studio instance using its execution role. The
boto3-from-laptop path is not reliable here (see 05 §6 / CLAUDE.md) — run these steps
**inside Studio**.

## 0. One-time seed (irreplaceable BART artifacts — dead host, D11)
Upload these local files to s3://aai-590-group2-capstone/eia-nowcast/… (S3 console UI or
from Studio). They cannot be regenerated:

| local file | S3 key (under eia-nowcast/) |
|---|---|
| data/raw/bart/od_2024.parquet | raw/bart/od_2024.parquet |
| data/reference/bart_stations.parquet | reference/bart_stations.parquet |
| data/reference/giants_home_2024.parquet | reference/giants_home_2024.parquet |
| data/gold/bart_attendance_calib_2024.parquet | gold/bart_attendance_calib_2024.parquet |
| data/transform/sf_daily_arrivals_2024.parquet | gold/sf_daily_arrivals_2024.parquet |
| data/transform/game_residuals_0_300m.parquet | gold/game_residuals_0_300m.parquet |
| data/gold/sf_food_services_daily_2024.parquet | gold/sf_food_services_daily_2024.parquet |

## 1. Clone + install
    git clone https://github.com/Giant-Leap-ai/eia-nowcast-pipeline.git
    cd eia-nowcast-pipeline
    pip install -e .          # Studio base image is pip/conda; uv optional

## 2. Configure (env, not committed)
    export S3_BUCKET=aai-590-group2-capstone
    export S3_PREFIX=eia-nowcast
    export AWS_REGION=<instance region>

## 3. Write smoke-test (gate — stop here if it fails)
    python -c "import boto3,os; boto3.client('s3').put_object(Bucket=os.environ['S3_BUCKET'], Key=os.environ['S3_PREFIX']+'/_smoketest', Body=b'ok'); print('S3 write OK')"

## 4. Run the runner
    python -m eia_pipeline.run_all --to-s3 --seasons 2024

## 5. Verify (list + read-back in place)
    python -c "from eia_pipeline.io import list_s3; [print(o['key']) for o in list_s3('eia-nowcast/gold/')]"
    python -c "import duckdb,os; b=os.environ['S3_BUCKET']; print(duckdb.sql(f\"SELECT count(*) FROM 's3://{b}/eia-nowcast/gold/cdtfa_food_services.parquet'\").pl())"
```

- [ ] **Step 2: Verify the runbook's local commands are copy-paste-correct**

Run (locally, the non-AWS parts): `uv run python -m eia_pipeline.run_all --seasons 2024`
Expected: matches runbook step 4 minus `--to-s3`; three tables land under `data/gold/`.
Eyeball the seed table: every listed local path exists — run
`for f in data/raw/bart/od_2024.parquet data/reference/bart_stations.parquet data/reference/giants_home_2024.parquet data/gold/bart_attendance_calib_2024.parquet data/transform/sf_daily_arrivals_2024.parquet data/transform/game_residuals_0_300m.parquet data/gold/sf_food_services_daily_2024.parquet; do test -f "$f" && echo "OK $f" || echo "MISSING $f"; done`
Expected: all `OK` (if any `MISSING`, fix the path in the runbook to the real location).

- [ ] **Step 3: Commit**

```bash
git add docs/06_aws_studio_runbook.md
git commit -m "docs: AWS Studio runbook + BART seed manifest (round 1)"
```

---

### Task 8: Open the PR to `main`

**Files:** none (git/PR only).

**Interfaces:**
- Consumes: all prior tasks committed on `task01-bart-oracle-inflow`.
- Produces: a PR for the repo owner to merge; after merge, Studio clones `main`.

- [ ] **Step 1: Push the branch**

```bash
git push -u origin task01-bart-oracle-inflow
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --base main --head task01-bart-oracle-inflow \
  --title "Round 1: Muni scrap + Studio→S3 gold runner" \
  --body "$(cat <<'EOF'
Cleans up and lands the gold layer to S3 from SageMaker Studio (round 1).

- Scrap Muni (code, capture job, ops, docs); log D12.
- gitignore .env / .env.*
- Add `python -m eia_pipeline.run_all --to-s3`: lands CDTFA, OI, MLB gold to
  s3://aai-590-group2-capstone/eia-nowcast/gold/ (BART-independent, live sources).
- docs/06 Studio runbook + BART seed manifest (BART hourly seeded once, D11 dead host).

Spec: docs/superpowers/specs/2026-07-09-sagemaker-studio-s3-landing-design.md
Owner action after merge: seed the BART artifacts (runbook §0), then run the runbook in Studio.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Confirm the PR exists**

Run: `gh pr view --json url,title,state`
Expected: prints the PR URL, the title above, and `"state": "OPEN"`.

---

## Post-merge (owner, outside this plan)

1. Seed the BART artifacts to S3 per runbook §0.
2. Run `docs/06_aws_studio_runbook.md` in Studio; confirm step 5 read-back returns rows.
3. Cancel the PeMS key at project submission.

## Fast-follows (not this round)

- Recompute `sf_food_services_daily` live in Studio (read seeded BART indicator from S3, join
  fresh CDTFA via `nowcast/disaggregate.py`).
- The real D11 fix: rewrite `ingest/bart.py` to read the capstone Gold `bart_daily_exits/`
  (1998→2025-07) + `bart_monthly_od/` feeds, enabling multi-year daily-grain modeling.
- Promote the runner into a Lambda (Lambda-first per CLAUDE.md) for scheduled refresh.
```
