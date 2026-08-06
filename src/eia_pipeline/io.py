"""IO helpers: land tidy frames to Parquet (local or S3) and query with DuckDB.
Keep signals in NATIVE units here — conversions happen in calibrate/.

S3 path uses boto3 (the reliable AWS call path here — see CLAUDE.md). Credentials
come from the profile named in AWS_PROFILE (loaded from .env via settings import)
or the default boto3 chain; nothing is hardcoded.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from .settings import settings


def s3_client():
    """A boto3 S3 client honoring AWS_PROFILE + AWS_REGION from the environment."""
    import boto3

    return boto3.Session(region_name=settings.aws_region).client("s3")


def land_parquet(df: pl.DataFrame, *parts: str, to_s3: bool = False) -> str:
    """Write a frame to Parquet. Local by default; S3 when to_s3=True.

    parts: path segments, e.g. land_parquet(df, "raw", "esmr", "facility=X.parquet")
    S3 writes go to s3://{S3_BUCKET}/{S3_PREFIX}/{parts...} (local copy kept too —
    the local cache is the working set; S3 is the shared/team copy).
    Returns the written path/URI.
    """
    path = settings.data_dir.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    if not to_s3:
        return str(path)
    uri = settings.s3_uri(*parts)
    bucket, key = _split_uri(uri)
    s3_client().upload_file(str(path), bucket, key)
    return uri


def fetch_s3(key: str, bucket: str | None = None, cache_subdir: str = "s3cache") -> Path:
    """Download s3://{bucket}/{key} once into data/{cache_subdir}/ and return the path.

    bucket defaults to settings.s3_bucket. Cached by key basename; delete the local
    file to force a re-pull. Use for shared inputs (e.g. the Dewey/Advan drops).
    """
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")
    dest = settings.data_dir / cache_subdir / Path(key).name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3_client().download_file(bucket, key, str(dest))
    return dest


def list_s3(prefix: str = "", bucket: str | None = None) -> list[dict]:
    """List objects under a prefix. Returns [{key, size, last_modified}, ...]."""
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")
    out: list[dict] = []
    paginator = s3_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            out.append({"key": o["Key"], "size": o["Size"], "last_modified": o["LastModified"]})
    return out


def _split_uri(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), uri
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def query(sql: str) -> pl.DataFrame:
    """Run a DuckDB SQL query (can SELECT over Parquet globs) and return Polars."""
    import duckdb

    return duckdb.sql(sql).pl()


def duckdb_s3(con=None):
    """Return a DuckDB connection wired to read `s3://` in place via the credential chain.

    DuckDB's httpfs does NOT inherit the boto3/instance-role credential chain
    automatically (unlike our boto3 helpers above), so it needs an explicit S3 secret
    — see docs/06_aws_studio_runbook.md §5. This centralizes that incantation so
    profiling and any future in-place S3 reads share one setup. Pass an existing `con`
    to configure it in place; otherwise a fresh in-memory connection is returned.

    Reads honor AWS_REGION from the environment (settings.aws_region).

    DUCKDB_MEMORY_LIMIT caps the connection. The panel build peaks on an 11.9M-row
    window-plus-ASOF join; DuckDB handles it fine, but an explicit limit makes it
    spill to disk under memory pressure instead of getting OOM-killed halfway
    through a Parquet write.
    """
    import os

    import duckdb

    con = con or duckdb.connect()
    con.sql("INSTALL aws; LOAD aws; INSTALL httpfs; LOAD httpfs;")
    con.sql(
        "CREATE SECRET IF NOT EXISTS eia_s3 "
        f"(TYPE S3, PROVIDER CREDENTIAL_CHAIN, REGION '{settings.aws_region}');"
    )
    limit = os.environ.get("DUCKDB_MEMORY_LIMIT")
    if limit:
        con.sql(f"SET memory_limit='{limit}'")
    return con
