"""Open-Meteo Historical Weather API ingester (build-now, keyless).
Endpoint: https://archive-api.open-meteo.com/v1/archive   License: CC-BY-4.0

Drives the wastewater flow residual and confounder control. ERA5-Land ~9km.
Needs plant lat/lon from schemas/venue_crosswalk.csv.
"""
from __future__ import annotations

import polars as pl

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Daily vars for the flow-residual model. Soil moisture is hourly in ERA5-Land →
# request hourly and aggregate to daily mean in the transform step.
DEFAULT_DAILY = [
    "precipitation_sum",
    "et0_fao_evapotranspiration",
    "temperature_2m_mean",
    "snowfall_sum",
]
DEFAULT_HOURLY = [
    "soil_moisture_7_to_28cm",
    "soil_moisture_28_to_100cm",
]


def fetch_weather(
    lat: float,
    lon: float,
    start: str,  # "YYYY-MM-DD"
    end: str,    # "YYYY-MM-DD"
    daily: list[str] | None = None,
    hourly: list[str] | None = None,
) -> pl.DataFrame:
    """Return a tidy daily weather frame for one location.

    Contract: columns include `date` plus the requested daily vars, and daily-mean
    aggregates of the requested hourly vars (e.g. soil_moisture_7_to_28cm_daymean).
    Native units (mm, degC, etc.). No API key required; be polite with call volume
    (free tier ~10k/day). Cache raw responses under data/raw/openmeteo/.
    """
    raise NotImplementedError(
        "Implement with a GET to ARCHIVE_URL (params: latitude, longitude, "
        "start_date, end_date, daily=..., hourly=..., timezone). Parse to Polars, "
        "aggregate hourly soil moisture to daily mean. See tasks/01_first_task.md."
    )
