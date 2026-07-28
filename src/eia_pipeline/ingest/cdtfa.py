"""CDTFA "Taxable Sales in California" — the business-location dollar anchor (build-now).
Source: CDTFA Open Data Portal, https://www.cdtfa.ca.gov/dataportal/
        machine-readable OData API: https://cdtfa.ca.gov/dataportal/api/odata/
        (note: host redirects www -> apex; hit the apex host directly)

This is the absolute-$ settlement anchor (dollar LEVEL) — sales-tax point-of-sale
("taxable transactions"), so it is a BUSINESS-LOCATION basis, quarterly. It is NOT the
primary target; it calibrates OI's %-lift into dollars (see calibrate/spend_velocity.py).

Endpoint used here: `Taxable_Sales_Counties` — county x quarter x BusinessType, with
`TaxableTransactions` in whole US dollars. The BusinessGroupCode taxonomy is:
    C01 Motor Vehicle & Parts   C02 Home Furnishings & Appliance
    C03 Building Material/Garden C04 Food & Beverage Stores (grocery)
    C05 Gasoline Stations       C06 Clothing & Accessories
    C07 General Merchandise     C08 Food Services & Drinking Places  <-- the `cdtfa_c08` line
    C09 Other Retail            CTR Total Retail & Food Services
    OTH All Other Outlets       TTL Total All Outlets
So the registered `cdtfa_c08` anchor == BusinessGroupCode 'C08' ("Food Services and
Drinking Places", NAICS 722) and is isolable at COUNTY grain. San Francisco is a
consolidated city-county, so the county row IS the city.

CAVEATS (verified 2026-07-01):
  - Grain is COUNTY-quarter. The sibling `Taxable_Sales_by_City` endpoint exists but only
    exposes Retail+Food-Services COMBINED and a grand Total per city — it does NOT isolate
    Food Services, so it is useless as the C08 anchor. Counties endpoint is strictly better.
  - Coverage for SAN FRANCISCO C08: contiguous 2015 Q1 .. 2025 Q4, no gaps, no suppression.
  - Lag ~2 quarters (2025 Q4 was live on 2026-07-01). Older records can be revised.
  - Suppression: rows carry a `DisclosureFlag`; when set the county/business cell is masked
    to protect a small number of permit-holders (not observed for SF, common for tiny
    county x category cells). We surface it as `disclosure_flag` and keep the row.
  - "Taxable transactions" excludes non-taxable sales (most groceries, some services), so
    C04/C08 are the taxable slice of restaurant/food spend, which is exactly what a
    sales-tax-anchored velocity wants.
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import requests

from ..settings import settings

# apex host (www 301-redirects to it); requests follows redirects but hit apex to be safe.
_BASE = "https://cdtfa.ca.gov/dataportal/api/odata"
COUNTIES_ENDPOINT = f"{_BASE}/Taxable_Sales_Counties"

# BusinessGroupCode -> human label (as returned by the API).
BUSINESS_GROUPS = {
    "C01": "Motor Vehicle and Parts Dealers",
    "C02": "Home Furnishings and Appliance Stores",
    "C03": "Building Material and Garden Equipment and Supplies Dealers",
    "C04": "Food and Beverage Stores",
    "C05": "Gasoline Stations",
    "C06": "Clothing and Clothing Accessories Stores",
    "C07": "General Merchandise Stores",
    "C08": "Food Services and Drinking Places",  # the registered cdtfa_c08 anchor (NAICS 722)
    "C09": "Other Retail Group",
    "CTR": "Total Retail and Food Services",
    "OTH": "All Other Outlets",
    "TTL": "Total All Outlets",
}
FOOD_SERVICES_CODE = "C08"

_RAW_DIR = settings.data_dir / "raw" / "cdtfa"


def _cached_odata(endpoint: str, odata_filter: str, cache_name: str) -> Path:
    """Fetch an OData collection (following $filter) once and cache the raw JSON.

    OData server-side paging is handled via @odata.nextLink; all pages are concatenated
    into a single {"value": [...]} document so the cache is a faithful full pull.
    """
    dest = _RAW_DIR / cache_name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    url: str | None = f"{endpoint}?$filter={odata_filter}"
    params: dict | None = None
    while url:
        r = requests.get(url, params=params, timeout=180)
        r.raise_for_status()
        doc = r.json()
        rows.extend(doc.get("value", []))
        url = doc.get("@odata.nextLink")
        params = None  # nextLink already carries the query string
    with open(dest, "w") as f:
        json.dump({"value": rows}, f)
    return dest


def fetch_county_taxable_sales(county: str = "SAN FRANCISCO") -> pl.DataFrame:
    """All taxable-sales rows for one county, NATIVE units (whole US dollars), tidy.

    Contract: one row per (year, quarter, business_group_code). Columns:
        geography (str)            county name, upper-case as the API returns it
        geo_level (str)            constant "county"
        business_group_code (str) e.g. 'C08'
        category (str)             human label, e.g. 'Food Services and Drinking Places'
        year (Int32), quarter (Int8, 1-4)
        taxable_sales_usd (Int64)  TaxableTransactions in whole dollars
        disclosure_flag (str|null) CDTFA suppression flag when the cell is masked
    Raw JSON cached under data/raw/cdtfa/. County name is matched case-insensitively.
    """
    county_u = county.upper()
    cache = _cached_odata(
        COUNTIES_ENDPOINT,
        odata_filter=f"County eq '{county_u}'",
        cache_name=f"counties_{county_u.replace(' ', '_').lower()}.json",
    )
    raw = json.loads(Path(cache).read_text())["value"]
    if not raw:
        return pl.DataFrame(
            schema={
                "geography": pl.Utf8, "geo_level": pl.Utf8, "business_group_code": pl.Utf8,
                "category": pl.Utf8, "year": pl.Int32, "quarter": pl.Int8,
                "taxable_sales_usd": pl.Int64, "disclosure_flag": pl.Utf8,
            }
        )
    return (
        pl.DataFrame(raw)
        .select(
            pl.col("County").cast(pl.Utf8).alias("geography"),
            pl.lit("county").alias("geo_level"),
            pl.col("BusinessGroupCode").cast(pl.Utf8).alias("business_group_code"),
            pl.col("BusinessType").cast(pl.Utf8).alias("category"),
            pl.col("CalendarYear").cast(pl.Int32).alias("year"),
            pl.col("Quarter").str.strip_prefix("Q").cast(pl.Int8).alias("quarter"),
            pl.col("TaxableTransactions").cast(pl.Int64).alias("taxable_sales_usd"),
            pl.col("DisclosureFlag").cast(pl.Utf8).alias("disclosure_flag"),
        )
        .sort("year", "quarter", "business_group_code")
    )


def fetch_food_services(county: str = "SAN FRANCISCO") -> pl.DataFrame:
    """Convenience: just the C08 'Food Services and Drinking Places' anchor line."""
    return fetch_county_taxable_sales(county).filter(
        pl.col("business_group_code") == FOOD_SERVICES_CODE
    )


if __name__ == "__main__":  # pragma: no cover
    df = fetch_county_taxable_sales("SAN FRANCISCO")
    print(df)
    print("C08 (Food Services & Drinking Places) only:")
    print(fetch_food_services("SAN FRANCISCO"))
