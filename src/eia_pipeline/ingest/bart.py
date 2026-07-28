"""BART hourly origin-destination ridership ingester (build-now, keyless, CC-BY).
Source: https://www.bart.gov/about/reports/ridership
        -> hourly OD portal: https://afcweb.bart.gov/ridership/origin-destination/

Per-year file `date-hour-soo-dest-YYYY.csv.gz` is a HEADERLESS CSV with columns:
    date (YYYY-MM-DD), hour (0-23), origin (4-char code), destination (4-char code),
    trip_count (riders who entered at origin and exited at destination in that hour).

For a venue: filter destination = venue station -> arrivals; origin = venue station ->
departures. Mode share is venue-dominated at door-served stations (COLS) and weak/
transfer-only at dense downtown stations (EMBR for Oracle Park) — treat accordingly.
Station codes are translated via the BART GTFS feed (keyless).
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import requests

from ..settings import settings

OD_URL_TEMPLATE = "https://afcweb.bart.gov/ridership/origin-destination/date-hour-soo-dest-{year}.csv.gz"
GTFS_URL = "https://www.bart.gov/dev/schedules/google_transit.zip"

OD_COLUMNS = ["date", "hour", "origin", "destination", "trip_count"]

_RAW_DIR = settings.data_dir / "raw" / "bart"


def _download(url: str, dest: Path, timeout: int = 180) -> Path:
    """Fetch `url` to `dest` once and cache it (be polite: no re-download if present)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


def fetch_od(year: int) -> pl.DataFrame:
    """Return the hourly OD table for a year.

    Contract: columns `date` (pl.Date), `hour` (Int8, 0-23), `origin`, `destination`
    (station codes), `trip_count` (Int32). One row per (date, hour, origin, dest) with
    a nonzero count. Raw .gz is cached under data/raw/bart/.
    """
    raw = _download(OD_URL_TEMPLATE.format(year=year), _RAW_DIR / f"date-hour-soo-dest-{year}.csv.gz")
    return (
        pl.read_csv(raw, has_header=False, new_columns=OD_COLUMNS)
        .with_columns(
            pl.col("date").str.to_date("%Y-%m-%d"),
            pl.col("hour").cast(pl.Int8),
            pl.col("trip_count").cast(pl.Int32),
        )
    )


def fetch_stations() -> pl.DataFrame:
    """Keyless station-code lookup from the BART GTFS feed.

    Contract: columns `station` (4-char code, e.g. EMBR), `station_name`, `lat`, `lon`.
    One row per station. Sourced from GTFS stops.txt (parent_station == code).
    """
    import io
    import zipfile

    raw = _download(GTFS_URL, _RAW_DIR / "google_transit.zip")
    with zipfile.ZipFile(raw) as z:
        stops = pl.read_csv(io.BytesIO(z.read("stops.txt")))
    return (
        stops.filter(pl.col("location_type") == 0)  # platforms only
        .group_by(pl.col("parent_station").alias("station"))
        .agg(
            pl.col("stop_name").first().alias("station_name"),
            pl.col("stop_lat").mean().alias("lat"),
            pl.col("stop_lon").mean().alias("lon"),
        )
        .sort("station")
    )
