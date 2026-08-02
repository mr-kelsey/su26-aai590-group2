"""Edge sets for the Tier 2 spatiotemporal graph. Three families, ablated separately.

Citywide, the hourly residual correlation between adjacent cells is about 0.11 at 250m, decaying to roughly 0.01 by 1km. That is a quarter of the 0.445 inside the dense 5km panel, and the gap tracks density rather than distance:

    pair density (min POI of the two)    mean r at <=500m
    dense (>=40)                          0.202
    mid   (20-39)                         0.102
    thin  (<20)                           0.055

So the graph carries information among the dense downtown cells and little among the thin outer ones, which makes a uniform distance kernel mis-specified. It also bounds how much Tier 2 can gain over the pooled Tier 1 GBM.

The three families we build:

  contiguity  queen adjacency on the grid. The naive spatial prior.
  distance    k-NN weighted by the empirical correlation function rather than an arbitrary Gaussian, and scaled by pair density as well, since the measurement above says density governs the coupling.
  flow        cosine over VISITOR_HOME_CBGS, which is where visitors come from. This is our OD-like signal and the closest analogue we have to a PeMS OD matrix. It is also the only family that can connect functionally linked cells that sit far apart.
"""
from __future__ import annotations

from pathlib import Path

from ..io import duckdb_s3
from ..settings import settings
from ..ingest.advan_bronze import bronze_patterns, SF_CITY_FILTER

CELL_DIM = "data/bronze_sf/cell_dim.parquet"
POI_CELL = "data/bronze_sf/poi_cell.parquet"

# Empirical citywide hourly correlation by inter-cell distance (see docstring).
# Used as the distance-edge weight; beyond ~1km it is indistinguishable from noise.
CORR_BY_DIST = [(250, 0.114), (500, 0.057), (750, 0.028), (1000, 0.013)]
MAX_EDGE_M = 1000

FLOW_WINDOW = ("2023-01-02", "2025-12-31")
FLOW_MIN_COS = 0.10
FLOW_TOP_K = 8


def _out(name: str) -> Path:
    p = settings.data_dir / "bronze_sf" / f"edges_{name}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_contiguity(con=None) -> Path:
    """Queen adjacency: cells touching on an edge or corner."""
    con = con or duckdb_s3()
    dest = _out("contiguity")
    con.execute(
        f"""
        COPY (
            SELECT a.unit_id AS src, b.unit_id AS dst, 1.0 AS w
            FROM read_parquet('{CELL_DIM}') a
            JOIN read_parquet('{CELL_DIM}') b
              ON abs(a.gi - b.gi) <= 1 AND abs(a.gj - b.gj) <= 1
             AND a.unit_id <> b.unit_id
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    _report(con, dest, "contiguity")
    return dest


def build_distance(con=None, max_m: int = MAX_EDGE_M) -> Path:
    """Distance edges weighted by the measured correlation function x pair density.

    The density scaling is the empirical part: coupling tracks how many POIs the
    thinner of the two cells has, not distance alone.
    """
    con = con or duckdb_s3()
    dest = _out("distance")
    cases = " ".join(
        f"WHEN d <= {m} THEN {r}" for m, r in CORR_BY_DIST
    )
    con.execute(
        f"""
        COPY (
            WITH p AS (
                SELECT a.unit_id AS src, b.unit_id AS dst,
                       sqrt(power((a.lat - b.lat) * 111320.0, 2)
                          + power((a.lon - b.lon) * 111320.0
                                  * cos(radians(37.78)), 2)) AS d,
                       least(a.n_poi, b.n_poi) AS min_poi
                FROM read_parquet('{CELL_DIM}') a
                JOIN read_parquet('{CELL_DIM}') b ON a.unit_id <> b.unit_id
            )
            SELECT src, dst, d AS dist_m, min_poi,
                   (CASE {cases} ELSE 0.0 END)
                   * least(min_poi / 40.0, 1.0) AS w
            FROM p
            WHERE d <= {max_m}
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    _report(con, dest, "distance")
    return dest


def build_flow(con=None, min_cos: float = FLOW_MIN_COS, top_k: int = FLOW_TOP_K) -> Path:
    """Cosine similarity between cells over visitor-home CBG distributions."""
    con = con or duckdb_s3()
    dest = _out("flow")
    s, e = FLOW_WINDOW
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _cellcbg AS
        -- VISITOR_HOME_CBGS is a VARCHAR JSON object {{cbg: count}}. It will not
        -- CAST to MAP directly; json_each is the idiom DuckDB accepts here.
        WITH kv AS (
            SELECT pc.unit_id, je.key AS cbg, CAST(je.value AS DOUBLE) AS n
            FROM read_parquet('{bronze_patterns()}') b
            JOIN read_parquet('{POI_CELL}') pc
              ON pc.footprint_id = b.FOOTPRINT_ID,
            json_each(b.VISITOR_HOME_CBGS) je
            WHERE {SF_CITY_FILTER} AND b.VISITOR_HOME_CBGS IS NOT NULL
              AND CAST(b.DATE_RANGE_START AS DATE) BETWEEN DATE '{s}' AND DATE '{e}'
        )
        SELECT unit_id, cbg, sum(n) AS n FROM kv GROUP BY 1, 2
        """
    )
    con.execute(
        f"""
        COPY (
            WITH norm AS (
                SELECT unit_id, sqrt(sum(n * n)) AS nrm FROM _cellcbg GROUP BY 1
            ),
            dot AS (
                SELECT a.unit_id AS src, b.unit_id AS dst, sum(a.n * b.n) AS dp
                FROM _cellcbg a JOIN _cellcbg b
                  ON a.cbg = b.cbg AND a.unit_id <> b.unit_id
                GROUP BY 1, 2
            ),
            cs AS (
                SELECT d.src, d.dst, d.dp / (na.nrm * nb.nrm) AS cos_sim
                FROM dot d JOIN norm na ON na.unit_id = d.src
                           JOIN norm nb ON nb.unit_id = d.dst
            ),
            rk AS (
                SELECT *, row_number() OVER (PARTITION BY src ORDER BY cos_sim DESC) AS rn
                FROM cs WHERE cos_sim >= {min_cos}
            )
            SELECT src, dst, cos_sim AS w FROM rk WHERE rn <= {top_k}
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    _report(con, dest, "flow")
    return dest


def _report(con, dest: Path, name: str) -> None:
    n, nodes, w = con.execute(
        f"""SELECT count(*), count(DISTINCT src), avg(w)
            FROM read_parquet('{dest}')"""
    ).fetchone()
    deg = con.execute(
        f"""SELECT round(avg(k),1), min(k), max(k) FROM (
                SELECT count(*) k FROM read_parquet('{dest}') GROUP BY src)"""
    ).fetchone()
    print(f"  edges_{name}: {n:,} directed | {nodes} nodes with >=1 edge | "
          f"mean w {w:.3f} | degree mean {deg[0]} [{deg[1]}, {deg[2]}]", flush=True)
