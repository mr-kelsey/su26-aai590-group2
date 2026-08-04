"""Generate src/data/pois.json: the server-side business search index.

One row per Advan POI inside the 452 modeled cells (same POI selection as
build-cells.py: last-week coordinates per FOOTPRINT_ID, CITY ILIKE
'San Francisco', SF bbox). The index is imported ONLY by the /api/places route;
clients receive at most the top 8 matches for their query. Note: Advan
anonymizes some POIs to their category name ("Full-Service Restaurants"), so
address search and the map-click pin are the universal fallbacks.

Run:
  cd /Users/Steve3/Projects/hyperfocus/venue-economics && \
  /Users/Steve3/Projects/personal/capstone/notebooks/.venv/bin/python scripts/build-poi-index.py
"""
import json
import os
from pathlib import Path

import duckdb

ADVAN_GLOB = os.environ.get(
    "ADVAN_GLOB",
    "/Users/Steve3/Projects/personal/capstone/S3/advan_weekly_patterns/*.parquet",
)
HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "src" / "data" / "pois.json"
CELLS = json.loads((HERE / "src" / "data" / "cells.json").read_text())

DLAT, DLON = CELLS["meta"]["dlat"], CELLS["meta"]["dlon"]
KEPT = {(c["gi"], c["gj"]) for c in CELLS["cells"]}

con = duckdb.connect()
rows = con.execute(
    f"""
    WITH poi AS (
        SELECT poi_name, addr, lat, lon FROM (
            SELECT LOCATION_NAME AS poi_name, STREET_ADDRESS AS addr,
                   LATITUDE AS lat, LONGITUDE AS lon,
                   row_number() OVER (PARTITION BY FOOTPRINT_ID
                                      ORDER BY DATE_RANGE_START DESC) AS rn
            FROM read_parquet('{ADVAN_GLOB}')
            WHERE CITY ILIKE 'San Francisco'
        ) WHERE rn = 1
    )
    SELECT poi_name, addr, lat, lon,
           CAST(floor(lat / {DLAT}) AS INTEGER) AS gi,
           CAST(floor(lon / {DLON}) AS INTEGER) AS gj
    FROM poi
    WHERE lat BETWEEN 37.70 AND 37.84 AND lon BETWEEN -122.52 AND -122.35
      AND poi_name IS NOT NULL
    ORDER BY poi_name, addr
    """
).fetchall()

seen = set()
pois = []
for name, addr, lat, lon, gi, gj in rows:
    if (gi, gj) not in KEPT:
        continue
    key = (name.strip().lower(), (addr or "").strip().lower())
    if key in seen:
        continue
    seen.add(key)
    pois.append({
        "n": name.strip(),
        "a": (addr or "").strip(),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
    })

assert len(pois) > 10_000, f"only {len(pois)} POIs; expected ~14K"

OUT.write_text(json.dumps({
    "meta": {"source": "Advan Weekly Patterns SF extract; POIs inside the modeled cells",
             "count": len(pois)},
    "pois": pois,
}, separators=(",", ":")) + "\n")
print(f"wrote {len(pois)} POIs -> {OUT} ({OUT.stat().st_size:,} bytes)")
