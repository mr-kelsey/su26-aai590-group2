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


def build_oi() -> pl.DataFrame:
    """OI Affinity SF-county daily card-spend index (%-shape anchor)."""
    return oi_tracker.county_daily_spend()


def build_mlb(seasons: list[int]) -> pl.DataFrame:
    """MLB Giants home schedule + attendance, one row per game, across seasons."""
    return pl.concat([mlb.fetch_home_schedule(s) for s in seasons], how="vertical")


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
