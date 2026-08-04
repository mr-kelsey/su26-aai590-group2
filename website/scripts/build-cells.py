"""Generate src/data/cells.json: the model's SF grid cells.

Mirrors the team pipeline's grid EXACTLY (su26-aai590-Group2
src/eia_pipeline/transform/spatial_units.py + ingest/advan_bronze.py):
  - POI set: one row per FOOTPRINT_ID, coordinates from the POI's most
    recent week, CITY ILIKE 'San Francisco', SF bbox as backstop.
  - Grid: unit_id = c{gi}_{gj}, gi = floor(lat/0.00225), gj = floor(lon/0.00284).
  - Keep cells with >= 10 POIs.
Cell ids are grid-derived, so they are stable across rebuilds and match the
model's unit_code values. Known tolerance: the team's poi_dim may differ by
a few edge POIs; true-up against cell_dim.parquet when the endpoint ships
(docs/PLUG-IN-ENDPOINT.md).

Run (needs duckdb; use the capstone notebooks venv):
  cd /Users/Steve3/Projects/hyperfocus/venue-economics && \
  /Users/Steve3/Projects/personal/capstone/notebooks/.venv/bin/python scripts/build-cells.py
"""
import json
import math
import os
from pathlib import Path

import duckdb

ADVAN_GLOB = os.environ.get(
    "ADVAN_GLOB",
    "/Users/Steve3/Projects/personal/capstone/S3/advan_weekly_patterns/*.parquet",
)
OUT = Path(__file__).resolve().parents[1] / "src" / "data" / "cells.json"

DLAT, DLON = 0.00225, 0.00284            # ~250m at 37.78N
LAT_MIN, LAT_MAX = 37.70, 37.84          # SF bbox backstop
LON_MIN, LON_MAX = -122.52, -122.35
MIN_POI = 10
VENUE_LAT, VENUE_LON = 37.7786, -122.3893  # Oracle Park home plate

con = duckdb.connect()
rows = con.execute(
    f"""
    WITH poi AS (
        SELECT footprint_id, lat, lon, naics FROM (
            SELECT FOOTPRINT_ID AS footprint_id,
                   LATITUDE AS lat, LONGITUDE AS lon,
                   NAICS_CODE AS naics,
                   row_number() OVER (PARTITION BY FOOTPRINT_ID
                                      ORDER BY DATE_RANGE_START DESC) AS rn
            FROM read_parquet('{ADVAN_GLOB}')
            WHERE CITY ILIKE 'San Francisco'
        ) WHERE rn = 1
    ),
    assigned AS (
        SELECT *, CAST(floor(lat / {DLAT}) AS INTEGER) AS gi,
                  CAST(floor(lon / {DLON}) AS INTEGER) AS gj
        FROM poi
        WHERE lat BETWEEN {LAT_MIN} AND {LAT_MAX}
          AND lon BETWEEN {LON_MIN} AND {LON_MAX}
    )
    SELECT gi, gj, count(*) AS n_poi,
           avg(lat) AS lat, avg(lon) AS lon,
           count(*) FILTER (WHERE naics LIKE '722%') AS n_food
    FROM assigned
    GROUP BY 1, 2 HAVING count(*) >= {MIN_POI}
    ORDER BY gi, gj
    """
).fetchall()

def dist_m(lat: float, lon: float) -> float:
    dy = (lat - VENUE_LAT) * 111320.0
    dx = (lon - VENUE_LON) * 111320.0 * math.cos(math.radians(VENUE_LAT))
    return math.sqrt(dx * dx + dy * dy)

cells = [
    {
        "id": f"c{gi}_{gj}", "gi": gi, "gj": gj,
        "lat": round(lat, 6), "lon": round(lon, 6),
        "n_poi": n_poi,
        "food_share": round(n_food / n_poi, 4),
        "dist_venue_m": round(dist_m(lat, lon)),
    }
    for gi, gj, n_poi, lat, lon, n_food in rows
]

assert len(cells) > 300, f"only {len(cells)} cells; expected ~452"
near = sum(1 for c in cells if c["dist_venue_m"] <= 600)
assert near >= 3, "no cells near Oracle Park; grid math is off"

OUT.write_text(json.dumps({
    "meta": {"dlat": DLAT, "dlon": DLON,
             "venue": {"lat": VENUE_LAT, "lon": VENUE_LON},
             "source": "Advan Weekly Patterns SF extract; grid per team spatial_units.py"},
    "cells": cells,
}, separators=(",", ":")) + "\n")
print(f"wrote {len(cells)} cells -> {OUT} ({near} within 600m of the park)")
