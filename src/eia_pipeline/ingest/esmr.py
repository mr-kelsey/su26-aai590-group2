"""California eSMR wastewater flow ingester (build-now, keyless).
Source: data.ca.gov "Water Quality - Effluent - eSMR Data" (CKAN DataStore + bulk).

For Task 01: pull one facility's daily `flow` series. Filter parameter=flow and the
facility serving the venue sewershed. Land to Parquet. Grain: facility x day (some
permits report monthly — check the facility's monitoring frequency).
"""
from __future__ import annotations

import polars as pl


def fetch_flow(
    facility: str,        # resolve from crosswalk (sewershed_plant_id / facility name)
    start: str,           # "YYYY-MM-DD"
    end: str,             # "YYYY-MM-DD"
) -> pl.DataFrame:
    """Return a tidy daily flow frame for one facility.

    Contract: columns `facility, date, value, unit` where value is flow in native
    units (typically MGD) and unit records it. One row per facility-date. If the
    permit reports monthly, surface that in `grain` metadata — do not fabricate daily.

    Implementation options:
      - CKAN DataStore SQL API (datastore_search_sql) for a filtered pull, OR
      - download the per-year Parquet and filter locally with DuckDB/Polars
        (often easier; plays to the local stack). See CLAUDE.md.
    """
    raise NotImplementedError(
        "Implement eSMR pull (CKAN DataStore API or bulk Parquet + DuckDB filter). "
        "Keep native units. See tasks/01_first_task.md."
    )
