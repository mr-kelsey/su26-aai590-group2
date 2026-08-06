"""Ordered runner for the data/bronze_sf rebuild (docs/PIPELINE.md steps 1 to 4).

The pipeline was written as a set of functions meant to be called once, by hand,
in a notebook. That is fine until `data/` is empty on a fresh machine and you have
to remember the order, which steps gate the ones after them, and what the row
counts are supposed to be. This module writes that down.

Run it from the repo root, with MEDALLION_ROOT pointing at a local mirror (or
S3_BUCKET set to read from S3):

    uv run python -m eia_pipeline.serve.build_panel --all

Roughly 5 minutes and 3-5 GB. The gates matter more than the speed: `verify`
proves our bronze explode still reproduces the team's silver exactly, and `units`
proves the grid still cuts into the same 452 cells. Either one failing means
something upstream changed shape and nothing downstream should be trusted.

PATH TRAP. panel.py and features.py READ through relative literals
("data/bronze_sf/...") but WRITE through settings.data_dir. Those only agree when
data_dir is the repo-root default and the process CWD is the repo root. We check
that here instead of letting it surface three steps later as a missing file.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..io import duckdb_s3
from ..settings import REPO_ROOT, settings

# What the panel must come out as. These are not guesses: they are the numbers
# docs/PIPELINE.md publishes, and every downstream result was measured on them.
EXPECT_CELLS = 452
EXPECT_POIS = 14_467
EXPECT_ROWS = 11_878_560          # 452 cells x 1,095 days x 24 hours
EXPECT_WINDOW = ("2023-01-02", "2025-12-31")
EXPECT_NONZERO = 0.725            # 72.5%, checked to the nearest half point


def _check_paths() -> None:
    """Fail loudly on the read-relative / write-absolute mismatch."""
    cwd = Path.cwd().resolve()
    if cwd != REPO_ROOT:
        raise RuntimeError(
            f"run from the repo root ({REPO_ROOT}), not {cwd}. panel.py reads via "
            "relative paths and writes via settings.data_dir; they only agree there."
        )
    if settings.data_dir.resolve() != (REPO_ROOT / "data").resolve():
        raise RuntimeError(
            f"DATA_DIR points at {settings.data_dir}, but the pipeline's read literals "
            "are hardcoded to ./data/bronze_sf. Unset DATA_DIR."
        )


def step_verify(con) -> None:
    """GATE: our bronze explode must reproduce the team's silver exactly."""
    from ..ingest.advan_bronze import verify_alignment

    r = verify_alignment(con=con)
    print(f"  verify_alignment: {r}", flush=True)
    if not r.get("ok"):
        raise RuntimeError(
            "bronze does not reproduce silver. The array-to-hour mapping or the "
            "source data changed shape; do not build on this."
        )


def step_poi(con) -> None:
    from ..ingest import advan_bronze

    advan_bronze.poi_dimension(con=con)


def step_hourly(con) -> None:
    from ..ingest import advan_bronze

    advan_bronze.explode_hourly(con=con)


def step_daily(con) -> None:
    from ..ingest import advan_bronze

    advan_bronze.explode_daily(con=con)


def step_units(con) -> None:
    """GATE: the grid must still cut into the same 452 cells.

    Everything downstream keys on unit_id, and `unit_code` is a dense rank over the
    sorted unit_id set. One cell crossing the >=10-POI threshold renumbers every
    cell after it, which no later step would notice.
    """
    from ..transform import spatial_units

    _, cell_dim = spatial_units.build(con=con)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{cell_dim}')").fetchone()[0]
    if n != EXPECT_CELLS:
        raise RuntimeError(
            f"grid produced {n} cells, expected {EXPECT_CELLS}. The unit_code map "
            "would shift and every published cell-level number would move with it."
        )


def step_cell_hour(con) -> None:
    from ..transform import panel

    panel.build_cell_hour(con=con)


def step_week_cov(con) -> None:
    from ..transform import panel

    panel.build_cell_week_coverage(con=con)


def step_cell_day(con) -> None:
    from ..transform import panel

    panel.build_cell_day(con=con)


def step_features(con) -> None:
    from ..transform import features

    features.build(con=con)


def step_baseline(con) -> None:
    from ..transform import features

    features.build_rolling_baseline_multi(con=con)


def step_check(con) -> None:
    """Final assertions. Everything downstream assumes all of these."""
    base = settings.data_dir / "bronze_sf"
    n_cells = con.execute(
        f"SELECT count(*) FROM read_parquet('{base}/cell_dim.parquet')"
    ).fetchone()[0]
    n_ch, nz = con.execute(
        f"""SELECT count(*), count(*) FILTER (WHERE person_hours > 0)
            FROM read_parquet('{base}/cell_hour.parquet')"""
    ).fetchone()
    n_mh, d0, d1 = con.execute(
        f"""SELECT count(*), min(date), max(date)
            FROM read_parquet('{base}/model_hour.parquet')"""
    ).fetchone()
    n_rb, miss = con.execute(
        f"""SELECT count(*), count(*) FILTER (WHERE base_k2 IS NULL)
            FROM read_parquet('{base}/rolling_baseline.parquet')"""
    ).fetchone()

    problems = []
    if n_cells != EXPECT_CELLS:
        problems.append(f"cell_dim {n_cells} != {EXPECT_CELLS}")
    if n_ch != EXPECT_ROWS:
        problems.append(f"cell_hour {n_ch:,} != {EXPECT_ROWS:,}")
    if n_mh != EXPECT_ROWS:
        problems.append(f"model_hour {n_mh:,} != {EXPECT_ROWS:,}")
    if n_rb != EXPECT_ROWS:
        problems.append(f"rolling_baseline {n_rb:,} != {EXPECT_ROWS:,}")
    if (str(d0), str(d1)) != EXPECT_WINDOW:
        problems.append(f"window {d0}..{d1} != {EXPECT_WINDOW[0]}..{EXPECT_WINDOW[1]}")
    if abs(nz / n_ch - EXPECT_NONZERO) > 0.005:
        problems.append(f"non-zero {100 * nz / n_ch:.1f}% != {100 * EXPECT_NONZERO:.1f}%")
    if problems:
        raise RuntimeError("panel does not match the published shape: " + "; ".join(problems))

    print(
        f"  OK  cells={n_cells}  rows={n_mh:,}  window={d0}..{d1}  "
        f"nonzero={100 * nz / n_ch:.1f}%  baseline_warmup={100 * miss / n_rb:.1f}%",
        flush=True,
    )


STEPS = [
    ("verify", step_verify),
    ("poi", step_poi),
    ("hourly", step_hourly),
    ("daily", step_daily),
    ("units", step_units),
    ("cell_hour", step_cell_hour),
    ("week_cov", step_week_cov),
    ("cell_day", step_cell_day),
    ("features", step_features),
    ("baseline", step_baseline),
    ("check", step_check),
]
STEP_NAMES = [n for n, _ in STEPS]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="run every step in order")
    ap.add_argument("--only", nargs="+", metavar="STEP", choices=STEP_NAMES,
                    help=f"run just these: {' '.join(STEP_NAMES)}")
    ap.add_argument("--from", dest="from_step", metavar="STEP", choices=STEP_NAMES,
                    help="resume from this step onward")
    args = ap.parse_args(argv)

    if args.only:
        wanted = [s for s in STEPS if s[0] in set(args.only)]
    elif args.from_step:
        wanted = STEPS[STEP_NAMES.index(args.from_step):]
    elif args.all:
        wanted = STEPS
    else:
        ap.error("pass --all, --only STEP..., or --from STEP")

    _check_paths()
    (settings.data_dir / "bronze_sf").mkdir(parents=True, exist_ok=True)
    con = duckdb_s3()
    import time

    for name, fn in wanted:
        t0 = time.perf_counter()
        print(f"[{name}]", flush=True)
        fn(con)
        print(f"  ({time.perf_counter() - t0:.1f}s)", flush=True)


if __name__ == "__main__":  # pragma: no cover
    main()
