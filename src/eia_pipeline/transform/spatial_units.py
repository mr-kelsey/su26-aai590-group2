"""POI -> spatial unit assignment.

We set the minimum number of POI's at 10 in determining our minimum quadrant size in breaking SF up into smaller unit blocks. Additionally, we measured the spatial correlation to determine the best distribution of cells.

Everything downstream joins on `unit_id` and is invariant to what a unit actually
is. Swapping 250m cells for 500m cells, census tracts, DataSF neighbourhoods, or
learned regions means rewriting only this module. v1 is pure grid cells; names get layered on later by relabelling
`cell_dim`, not by rebuilding anything.
"""
from __future__ import annotations

from pathlib import Path

from ..io import duckdb_s3
from ..settings import settings

# ~250m at 37.78degN. 1 deg lat ~ 111.32km; 1 deg lon ~ 111.32km * cos(37.78deg).
GRID_DLAT = 0.00225
GRID_DLON = 0.00284

# San Francisco county bounding box. Advan's CITY field is the primary filter;
# this is a geographic backstop against mislabelled rows.
SF_LAT_MIN, SF_LAT_MAX = 37.70, 37.84
SF_LON_MIN, SF_LON_MAX = -122.52, -122.35

MIN_POI_PER_CELL = 10

# Oracle Park home plate. The same origin silver uses, so ring-based results in
# gold stay directly comparable with distances computed here.
VENUE_LAT, VENUE_LON = 37.7786, -122.3893

POI_DIM = "data/bronze_sf/poi_dim.parquet"


def build(con=None, dlat: float = GRID_DLAT, dlon: float = GRID_DLON,
          min_poi: int = MIN_POI_PER_CELL) -> tuple[Path, Path]:
    """Write poi_cell.parquet (POI -> unit_id) and cell_dim.parquet (unit features).

    Returns both paths. Prints the retained/dropped split so coverage loss is on
    the record rather than discovered later.
    """
    con = con or duckdb_s3()
    out = settings.data_dir / "bronze_sf"
    out.mkdir(parents=True, exist_ok=True)
    poi_cell, cell_dim = out / "poi_cell.parquet", out / "cell_dim.parquet"

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _assigned AS
        SELECT footprint_id, lat, lon, naics, top_category,
               CAST(floor(lat / {dlat}) AS INTEGER) AS gi,
               CAST(floor(lon / {dlon}) AS INTEGER) AS gj
        FROM read_parquet('{POI_DIM}')
        WHERE lat BETWEEN {SF_LAT_MIN} AND {SF_LAT_MAX}
          AND lon BETWEEN {SF_LON_MIN} AND {SF_LON_MAX}
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _kept AS
        SELECT gi, gj, count(*) AS n_poi
        FROM _assigned GROUP BY 1, 2 HAVING count(*) >= {min_poi}
        """
    )
    # unit_id is derived from the grid indices, so it is stable across rebuilds
    # and carries no dependency on row order.
    con.execute(
        f"""
        COPY (
            SELECT a.footprint_id,
                   printf('c%d_%d', a.gi, a.gj) AS unit_id,
                   a.gi, a.gj
            FROM _assigned a JOIN _kept k USING (gi, gj)
        ) TO '{poi_cell}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    con.execute(
        f"""
        COPY (
            WITH agg AS (
                SELECT a.gi, a.gj, count(*) AS n_poi,
                       avg(a.lat) AS lat, avg(a.lon) AS lon,
                       count(*) FILTER (WHERE a.naics LIKE '722%') AS n_food,
                       count(*) FILTER (WHERE a.naics LIKE '44%'
                                           OR a.naics LIKE '45%') AS n_retail,
                       count(*) FILTER (WHERE a.naics LIKE '721%') AS n_lodging
                FROM _assigned a JOIN _kept k USING (gi, gj)
                GROUP BY 1, 2
            )
            SELECT printf('c%d_%d', gi, gj) AS unit_id, gi, gj, lat, lon,
                   n_poi, n_food, n_retail, n_lodging,
                   n_food::DOUBLE / n_poi AS food_share,
                   -- equirectangular metres; exact enough over a 10km city
                   sqrt(power((lat - {VENUE_LAT}) * 111320.0, 2)
                      + power((lon - ({VENUE_LON})) * 111320.0
                              * cos(radians({VENUE_LAT})), 2)) AS dist_venue_m,
                   (degrees(atan2((lon - ({VENUE_LON}))
                                  * cos(radians({VENUE_LAT})),
                                  lat - {VENUE_LAT})) + 360.0) % 360.0
                       AS bearing_venue_deg
            FROM agg
        ) TO '{cell_dim}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    tot, kept, cells = con.execute(
        f"""
        SELECT (SELECT count(*) FROM _assigned),
               (SELECT count(*) FROM read_parquet('{poi_cell}')),
               (SELECT count(*) FROM read_parquet('{cell_dim}'))
        """
    ).fetchone()
    print(
        f"  cells: {cells} (>={min_poi} POIs) | POIs kept {kept:,}/{tot:,} "
        f"({100 * kept / tot:.1f}%) | dropped {tot - kept:,} in thin cells",
        flush=True,
    )
    return poi_cell, cell_dim
