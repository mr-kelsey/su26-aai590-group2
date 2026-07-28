"""Opportunity Insights Economic Tracker — Affinity consumer card spend (build-now).
Source: https://github.com/OpportunityInsights/EconomicTracker  (public, cite provider)

County-level file gives TOTAL card spend as a fractional deviation from a Jan-2020
baseline (`spend_all`; e.g. 0.044 = +4.4% vs baseline) — an index, NOT dollars, and no
category split at county grain. It supplies the high-frequency *shape/lift*; absolute
dollars come from CDTFA (see calibrate/spend_velocity.py).

COVERAGE (verified 2026-07-01): county file spans 2018-12 .. 2024-06, BUT the county
DAILY (`freq='d'`) series ends ~2022-06; later dates are weekly (`freq='w'`). Overlap
with Giants attendance at daily grain is therefore the 2021 + early-2022 home slate.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from ..settings import settings

COUNTY_DAILY_URL = (
    "https://raw.githubusercontent.com/OpportunityInsights/EconomicTracker/main/"
    "data/Affinity%20-%20County%20-%20Daily.csv"
)
SF_COUNTY_FIPS = "6075"
_CACHE = settings.data_dir / "raw" / "oi" / "Affinity_County_Daily.csv"


def _cached_csv() -> Path:
    if _CACHE.exists() and _CACHE.stat().st_size > 0:
        return _CACHE
    import requests

    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(COUNTY_DAILY_URL, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(_CACHE, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return _CACHE


def county_daily_spend(countyfips: str = SF_COUNTY_FIPS) -> pl.DataFrame:
    """Daily total-spend index for one county: columns date (pl.Date), spend_idx (Float64).

    spend_idx is the fractional deviation vs Jan-2020 baseline. Only `freq='d'` rows with
    a non-missing value are returned (small-county/day cells are suppressed as '.').
    """
    raw = pl.read_csv(_cached_csv(), infer_schema_length=0)  # mixed types; parse as str then cast
    return (
        raw.filter((pl.col("countyfips") == countyfips) & (pl.col("freq") == "d"))
        .with_columns(
            pl.date(
                pl.col("year").cast(pl.Int32),
                pl.col("month").cast(pl.Int32),
                pl.col("day").cast(pl.Int32),
            ).alias("date"),
            pl.col("spend_all").cast(pl.Float64, strict=False).alias("spend_idx"),
        )
        .drop_nulls("spend_idx")
        .select("date", "spend_idx")
        .sort("date")
    )
