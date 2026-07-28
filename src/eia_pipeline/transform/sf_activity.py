"""Broad daily San-Francisco activity indicator from BART arrivals.

For temporal disaggregation (docs/02 rung 1) we need a DAILY indicator that (a) is defined
on every day, not just event days, and (b) plausibly tracks daily economic activity in SF.
Total BART arrivals *into* SF stations fits: it carries the weekday commute, the weekend
dip, holidays, and event bumps — a general "people came into SF today" proxy. The event
signal (e.g., a Giants game) rides on top of this baseline; it is NOT the whole indicator.

This is the high-frequency shape that will distribute a slow CDTFA quarterly dollar total
across days. It is a proxy, not dollars — proportionality to spend is assumed only as well
as the disaggregation's indicator step allows (see docs/04 caveats).
"""
from __future__ import annotations

import polars as pl

# The 8 BART stations physically in San Francisco county. NOTE: Daly City (DALY) sits just
# over the line in San Mateo county and is deliberately excluded.
SF_STATIONS = ["EMBR", "MONT", "POWL", "CIVC", "16TH", "24TH", "GLEN", "BALB"]


def daily_sf_arrivals(od: pl.DataFrame, stations: list[str] | None = None) -> pl.DataFrame:
    """Total daily BART arrivals into SF: columns date (pl.Date), sf_arrivals (Int64).

    `od` is a BART hourly OD frame (date, hour, origin, destination, trip_count).
    Arrivals = trips whose destination is an SF station, summed over all hours and stations.
    """
    stations = stations or SF_STATIONS
    return (
        od.filter(pl.col("destination").is_in(stations))
        .group_by("date")
        .agg(pl.col("trip_count").sum().alias("sf_arrivals"))
        .sort("date")
    )


def daily_sf_arrivals_multiyear(years: list[int]) -> pl.DataFrame:
    """Convenience: fetch BART OD for each year and return the concatenated daily indicator.

    Pulls (and caches) each year's OD via ingest.bart.fetch_od. Heavy (~9M rows/yr) — call
    only for the span you actually need to match the CDTFA quarters being disaggregated.
    """
    from ..ingest import bart

    frames = [daily_sf_arrivals(bart.fetch_od(y)) for y in years]
    return pl.concat(frames).sort("date")
