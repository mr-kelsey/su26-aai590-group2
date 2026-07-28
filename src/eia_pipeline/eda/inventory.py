"""Inventory + profile the S3 medallion layers (bronze / silver / gold).

Discovery-first EDA: enumerate every object under the top-level `bronze/ silver/ gold/`
prefixes, then profile each Parquet table *in place* over `s3://` (schema, row count,
per-column null rate, and the min/max of any date/timestamp column). We land the
SUMMARY tables + a markdown report to `data/eda/` — never the raw layer data itself.

Why top-level prefixes bypass settings.s3_prefix: the medallion layers live at the
bucket ROOT (s3://<bucket>/bronze/…), not under the legacy `eia-nowcast/` prefix that
`settings.s3_uri()` prepends. So inventory hits `list_s3("bronze/")` directly and
profiling builds `s3://<bucket>/<key>` URIs from the raw key.

Two profiling depths:
  - run_fast()  DEFAULT — dataset grain, Parquet FOOTERS only (row counts + schema).
                Finishes in seconds, can't OOM. Use this over big buckets (the Advan
                part-file datasets have hundreds of multi-MB files).
  - run()       DEEP — per-file null rates + date ranges (full column scans). Only for
                small single-file tables; pathological over many part-files.

Run inside SageMaker Studio (the execution role reaches the bucket; laptop boto3 does
not — see docs/06_aws_studio_runbook.md):

    python -m eia_pipeline.eda.inventory              # fast dataset-grain (default)
    python -m eia_pipeline.eda.inventory --to-s3      # + mirror to gold/_eda/
    python -m eia_pipeline.eda.inventory --deep       # slow per-file null/date profile

or from a notebook cell:

    from eia_pipeline.eda.inventory import run_fast
    inv, ds = run_fast()          # ds = one row per dataset (advan_hourly, etc.)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from ..io import duckdb_s3, land_parquet, list_s3, s3_client
from ..settings import settings

LAYERS = ("bronze", "silver", "gold")

# Columns whose name (case-insensitive) OR type marks them as the table's time axis.
_DATE_NAME_HINTS = ("date", "datetime", "timestamp", "ts", "day", "time")
_EDA_SUBDIR = ("eda",)  # under data/  -> data/eda/


def _uri(bucket: str, key: str) -> str:
    """Build the s3:// URI for an object key. Factored out so tests can redirect
    profiling to local Parquet fixtures."""
    return f"s3://{bucket}/{key}"


def _dataset_key(key: str) -> str:
    """Collapse a Parquet object key to its logical DATASET.

    A depth-2 key (`layer/table.parquet`) is a standalone table — returned as-is.
    A deeper key (`layer/subdir/…/part.parquet`) belongs to a multi-file dataset
    keyed by `layer/subdir` (e.g. every `silver/advan_hourly/<week>.parquet` rolls
    up to `silver/advan_hourly`). This is what lets the fast profiler treat hundreds
    of weekly part-files as ONE logical table instead of hundreds of objects.
    """
    parts = key.split("/")
    if len(parts) <= 2:
        return key
    return "/".join(parts[:2])


# --------------------------------------------------------------------------- #
# 1. Inventory — cheap, boto3, no httpfs secret needed
# --------------------------------------------------------------------------- #
def inventory(layers: tuple[str, ...] = LAYERS, bucket: str | None = None) -> pl.DataFrame:
    """One row per S3 object across the given layers.

    Columns: layer, key, filename, ext, size_bytes, size_mb, last_modified.
    Empty layers simply contribute no rows (reported later by render_report).
    """
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")

    rows: list[dict] = []
    for layer in layers:
        for o in list_s3(prefix=f"{layer}/", bucket=bucket):
            key = o["key"]
            name = Path(key).name
            # Skip directory-marker pseudo-objects (zero-byte keys ending in '/').
            if key.endswith("/"):
                continue
            rows.append(
                {
                    "layer": layer,
                    "key": key,
                    "filename": name,
                    "ext": Path(name).suffix.lower().lstrip("."),
                    "size_bytes": o["size"],
                    "size_mb": round(o["size"] / 1_048_576, 3),
                    "last_modified": o["last_modified"],
                }
            )
    schema = {
        "layer": pl.Utf8,
        "key": pl.Utf8,
        "filename": pl.Utf8,
        "ext": pl.Utf8,
        "size_bytes": pl.Int64,
        "size_mb": pl.Float64,
        "last_modified": pl.Datetime(time_zone="UTC"),
    }
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema).sort(["layer", "key"])


# --------------------------------------------------------------------------- #
# 2. Profile — DuckDB in place over s3://
# --------------------------------------------------------------------------- #
def profile_parquet(uri: str, con) -> dict:
    """Profile a single Parquet dataset at `uri` (an `s3://…` object or local path)
    using an existing DuckDB connection.

    Returns a dict with n_rows, n_cols, a per-column list (name/dtype/null rate), and
    the detected date column's min/max (None if no date-like column). Any read error is
    captured in the `error` field so one bad object never aborts the whole run.
    """
    out: dict = {
        "uri": uri,
        "n_rows": None,
        "n_cols": None,
        "date_col": None,
        "date_min": None,
        "date_max": None,
        "columns": [],
        "error": None,
    }
    try:
        # DESCRIBE is metadata-only (cheap); it also works for a directory of part files
        # when the key is a dataset root, though here keys are individual objects.
        desc = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{uri}')").pl()
        col_names = desc["column_name"].to_list()
        col_types = desc["column_type"].to_list()
        out["n_cols"] = len(col_names)

        n_rows = con.sql(f"SELECT count(*) AS n FROM read_parquet('{uri}')").pl()["n"][0]
        out["n_rows"] = int(n_rows)

        # Per-column null rate in one pass.
        null_exprs = ", ".join(
            f"sum(CASE WHEN \"{c}\" IS NULL THEN 1 ELSE 0 END) AS \"{c}\"" for c in col_names
        )
        nulls = (
            con.sql(f"SELECT {null_exprs} FROM read_parquet('{uri}')").pl()
            if col_names
            else pl.DataFrame()
        )
        cols_meta = []
        for c, t in zip(col_names, col_types):
            n_null = int(nulls[c][0]) if col_names else 0
            cols_meta.append(
                {
                    "column": c,
                    "dtype": t,
                    "n_null": n_null,
                    "null_rate": round(n_null / n_rows, 4) if n_rows else None,
                }
            )
        out["columns"] = cols_meta

        date_col = _pick_date_column(col_names, col_types)
        if date_col is not None:
            mm = con.sql(
                f"SELECT min(\"{date_col}\") AS lo, max(\"{date_col}\") AS hi "
                f"FROM read_parquet('{uri}')"
            ).pl()
            out["date_col"] = date_col
            out["date_min"] = str(mm["lo"][0]) if mm["lo"][0] is not None else None
            out["date_max"] = str(mm["hi"][0]) if mm["hi"][0] is not None else None
    except Exception as e:  # noqa: BLE001 — deliberately per-object soft failure
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _pick_date_column(names: list[str], types: list[str]) -> str | None:
    """Choose the table's time axis: prefer a DATE/TIMESTAMP-typed column, else a
    name hint (date/datetime/timestamp/ts/day/time). Returns None if neither exists."""
    typed = [
        n
        for n, t in zip(names, types)
        if t.upper().startswith(("DATE", "TIMESTAMP"))
    ]
    if typed:
        return typed[0]
    for n in names:
        low = n.lower()
        if any(h in low for h in _DATE_NAME_HINTS):
            return n
    return None


def profile_all(inv_df: pl.DataFrame, bucket: str | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Profile every `.parquet` object in the inventory.

    Returns (table_profile, column_profile):
      table_profile — one row per object: layer, key, n_rows, n_cols, date_col,
                      date_min, date_max, size_mb, status ('ok' | 'skipped' | 'error').
      column_profile — one row per (key, column): layer, key, column, dtype, n_null,
                       null_rate. Only for successfully-profiled Parquet objects.
    """
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")

    if inv_df.is_empty():
        return _empty_table_profile(), _empty_column_profile()

    con = duckdb_s3()
    table_rows: list[dict] = []
    col_rows: list[dict] = []
    for r in inv_df.iter_rows(named=True):
        layer, key, ext = r["layer"], r["key"], r["ext"]
        base = {"layer": layer, "key": key, "size_mb": r["size_mb"]}
        if ext != "parquet":
            table_rows.append(
                {**base, "n_rows": None, "n_cols": None, "date_col": None,
                 "date_min": None, "date_max": None, "status": "skipped", "note": f"non-parquet (.{ext or '?'})"}
            )
            continue
        p = profile_parquet(_uri(bucket, key), con)
        status = "error" if p["error"] else "ok"
        table_rows.append(
            {
                **base,
                "n_rows": p["n_rows"],
                "n_cols": p["n_cols"],
                "date_col": p["date_col"],
                "date_min": p["date_min"],
                "date_max": p["date_max"],
                "status": status,
                "note": p["error"] or "",
            }
        )
        for c in p["columns"]:
            col_rows.append({"layer": layer, "key": key, **c})

    table_profile = (
        pl.DataFrame(table_rows).sort(["layer", "key"]) if table_rows else _empty_table_profile()
    )
    column_profile = (
        pl.DataFrame(col_rows).sort(["layer", "key", "column"]) if col_rows else _empty_column_profile()
    )
    return table_profile, column_profile


def _empty_table_profile() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "layer": pl.Utf8, "key": pl.Utf8, "size_mb": pl.Float64,
            "n_rows": pl.Int64, "n_cols": pl.Int64, "date_col": pl.Utf8,
            "date_min": pl.Utf8, "date_max": pl.Utf8, "status": pl.Utf8, "note": pl.Utf8,
        }
    )


def _empty_column_profile() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "layer": pl.Utf8, "key": pl.Utf8, "column": pl.Utf8,
            "dtype": pl.Utf8, "n_null": pl.Int64, "null_rate": pl.Float64,
        }
    )


# --------------------------------------------------------------------------- #
# 2b. Fast profile — dataset grain, Parquet FOOTERS only (no column scan)
# --------------------------------------------------------------------------- #
def profile_fast(inv_df: pl.DataFrame, bucket: str | None = None) -> pl.DataFrame:
    """Profile the medallion at DATASET grain reading only Parquet metadata.

    Unlike `profile_all` (which scans every column of every file for null rates and
    date ranges — pathologically slow and memory-heavy over hundreds of S3 part-files),
    this reads each file's FOOTER only: row counts from `parquet_file_metadata`, schema
    width from a `DESCRIBE` on one representative file. No column data crosses the wire,
    so a full run finishes in seconds and can't OOM the instance.

    Returns one row per dataset: layer, dataset, n_files, total_mb, n_rows, n_cols,
    status ('ok' | 'error'), note. `n_rows` is summed across the dataset's files.
    """
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")

    pq = inv_df.filter(pl.col("ext") == "parquet") if not inv_df.is_empty() else inv_df
    if pq.is_empty():
        return _empty_fast_profile()

    pq = pq.with_columns(
        pl.col("key").map_elements(_dataset_key, return_dtype=pl.Utf8).alias("dataset")
    )
    con = duckdb_s3()
    rows: list[dict] = []
    for (dataset,), grp in pq.group_by(["dataset"], maintain_order=True):
        keys = grp["key"].to_list()
        uris = [_uri(bucket, k) for k in keys]
        rec = {
            "layer": grp["layer"][0],
            "dataset": dataset,
            "n_files": len(keys),
            "total_mb": round(float(grp["size_mb"].sum()), 3),
            "n_rows": None,
            "n_cols": None,
            "status": "ok",
            "note": "" if len(keys) == 1 else f"{len(keys)} files (schema from first)",
        }
        try:
            uri_list = ", ".join("'" + u + "'" for u in uris)
            nr = con.sql(
                f"SELECT sum(num_rows) AS n FROM parquet_file_metadata([{uri_list}])"
            ).pl()["n"][0]
            rec["n_rows"] = int(nr) if nr is not None else None
            desc = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{uris[0]}')").pl()
            rec["n_cols"] = desc.height
        except Exception as e:  # noqa: BLE001 — per-dataset soft failure
            rec["status"] = "error"
            rec["note"] = f"{type(e).__name__}: {e}"
        rows.append(rec)

    return pl.DataFrame(rows).sort(["layer", "dataset"])


def _empty_fast_profile() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "layer": pl.Utf8, "dataset": pl.Utf8, "n_files": pl.Int64,
            "total_mb": pl.Float64, "n_rows": pl.Int64, "n_cols": pl.Int64,
            "status": pl.Utf8, "note": pl.Utf8,
        }
    )


def render_report_fast(inv_df: pl.DataFrame, ds_df: pl.DataFrame, bucket: str) -> str:
    """Dataset-grain markdown report (companion to the fast profiler)."""
    total_mb = inv_df["size_bytes"].sum() / 1_048_576 if not inv_df.is_empty() else 0
    lines: list[str] = [
        "# EDA — S3 medallion inventory (dataset grain, metadata-only)",
        "",
        f"- **Bucket:** `s3://{bucket}/`",
        f"- **Objects:** {inv_df.height} · **datasets:** {ds_df.height} · {total_mb:.1f} MB total",
        "",
    ]
    for layer in LAYERS:
        ld = ds_df.filter(pl.col("layer") == layer) if not ds_df.is_empty() else ds_df
        lines.append(f"## {layer}")
        if ld.is_empty():
            lines.append("_no objects_\n")
            continue
        lines.append("| dataset | files | rows | cols | size (MB) | status |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for r in ld.iter_rows(named=True):
            rows = f"{r['n_rows']:,}" if r["n_rows"] is not None else "—"
            cols = r["n_cols"] if r["n_cols"] is not None else "—"
            status = r["status"] + (f" ({r['note']})" if r["note"] and r["status"] != "ok" else "")
            lines.append(
                f"| `{r['dataset']}` | {r['n_files']} | {rows} | {cols} | {r['total_mb']} | {status} |"
            )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 3. Report
# --------------------------------------------------------------------------- #
def render_report(inv_df: pl.DataFrame, table_df: pl.DataFrame, bucket: str) -> str:
    """Render a human-readable markdown summary grouped by layer."""
    lines: list[str] = [
        "# EDA — S3 medallion inventory & profile",
        "",
        f"- **Bucket:** `s3://{bucket}/`",
        f"- **Layers:** {', '.join(LAYERS)} (top-level prefixes)",
        f"- **Objects:** {inv_df.height}"
        + (f" ({inv_df['size_bytes'].sum() / 1_048_576:.1f} MB total)" if not inv_df.is_empty() else ""),
        "",
    ]
    for layer in LAYERS:
        li = inv_df.filter(pl.col("layer") == layer) if not inv_df.is_empty() else inv_df
        lt = table_df.filter(pl.col("layer") == layer) if not table_df.is_empty() else table_df
        lines.append(f"## {layer}")
        if li.is_empty():
            lines.append("_no objects_\n")
            continue
        mb = li["size_bytes"].sum() / 1_048_576
        n_ok = lt.filter(pl.col("status") == "ok").height if not lt.is_empty() else 0
        n_skip = lt.filter(pl.col("status") == "skipped").height if not lt.is_empty() else 0
        n_err = lt.filter(pl.col("status") == "error").height if not lt.is_empty() else 0
        lines.append(
            f"{li.height} objects · {mb:.1f} MB · profiled {n_ok} parquet"
            + (f" · skipped {n_skip}" if n_skip else "")
            + (f" · **{n_err} error(s)**" if n_err else "")
        )
        lines.append("")
        lines.append("| key | rows | cols | date range | size (MB) | status |")
        lines.append("|---|---:|---:|---|---:|---|")
        src = lt if not lt.is_empty() else li
        for r in src.iter_rows(named=True):
            key = r["key"]
            rows = f"{r['n_rows']:,}" if r.get("n_rows") is not None else "—"
            cols = r.get("n_cols") if r.get("n_cols") is not None else "—"
            dr = (
                f"{r['date_min']} → {r['date_max']}"
                if r.get("date_min") and r.get("date_max")
                else "—"
            )
            size = r.get("size_mb", "—")
            status = r.get("status", "")
            note = r.get("note") or ""
            status_cell = status + (f" ({note})" if note and status != "ok" else "")
            lines.append(f"| `{key}` | {rows} | {cols} | {dr} | {size} | {status_cell} |")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 4. Orchestration
# --------------------------------------------------------------------------- #
def run(
    layers: tuple[str, ...] = LAYERS,
    bucket: str | None = None,
    to_s3: bool = False,
    verbose: bool = True,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Inventory -> (print) -> profile -> land summaries + report. Returns
    (inventory, table_profile, column_profile)."""
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")

    inv = inventory(layers=layers, bucket=bucket)
    if verbose:
        print(f"Inventory: {inv.height} object(s) under s3://{bucket}/ [{', '.join(layers)}]")
        with pl.Config(tbl_rows=50, tbl_cols=-1, fmt_str_lengths=60):
            print(inv.select("layer", "key", "size_mb", "last_modified"))

    table_df, col_df = profile_all(inv, bucket=bucket)
    report = render_report(inv, table_df, bucket)

    # Land SUMMARIES only (never raw layer data) under data/eda/.
    inv_path = land_parquet(inv, *_EDA_SUBDIR, "inventory.parquet")
    tbl_path = land_parquet(table_df, *_EDA_SUBDIR, "table_profile.parquet")
    col_path = land_parquet(col_df, *_EDA_SUBDIR, "column_profile.parquet")
    report_path = settings.data_dir.joinpath(*_EDA_SUBDIR, "EDA_bronze_silver_gold.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    if verbose:
        print(f"\nLanded:\n  {inv_path}\n  {tbl_path}\n  {col_path}\n  {report_path}")
        print("\n" + report)

    if to_s3:
        # Mirror the summaries + report to gold/_eda/ (top-level, no eia-nowcast prefix).
        client = s3_client()
        for local, name in [
            (inv_path, "inventory.parquet"),
            (tbl_path, "table_profile.parquet"),
            (col_path, "column_profile.parquet"),
            (str(report_path), "EDA_bronze_silver_gold.md"),
        ]:
            client.upload_file(str(local), bucket, f"gold/_eda/{name}")
        if verbose:
            print(f"\nMirrored summaries + report to s3://{bucket}/gold/_eda/")

    return inv, table_df, col_df


def run_fast(
    layers: tuple[str, ...] = LAYERS,
    bucket: str | None = None,
    to_s3: bool = False,
    verbose: bool = True,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Inventory -> dataset-grain metadata profile -> land + report. Returns
    (inventory, dataset_profile). Footer-only, so it finishes in seconds and can't OOM
    — the safe default over big buckets (e.g. the Advan part-file datasets)."""
    bucket = bucket or settings.s3_bucket
    if not bucket:
        raise RuntimeError("S3_BUCKET not set — see .env.example")

    inv = inventory(layers=layers, bucket=bucket)
    ds = profile_fast(inv, bucket=bucket)
    report = render_report_fast(inv, ds, bucket)

    inv_path = land_parquet(inv, *_EDA_SUBDIR, "inventory.parquet")
    ds_path = land_parquet(ds, *_EDA_SUBDIR, "dataset_profile.parquet")
    report_path = settings.data_dir.joinpath(*_EDA_SUBDIR, "EDA_datasets.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)

    if verbose:
        print(f"Inventory: {inv.height} object(s) -> {ds.height} dataset(s) under s3://{bucket}/")
        with pl.Config(tbl_rows=100, tbl_cols=-1, fmt_str_lengths=70):
            print(ds)
        print(f"\nLanded:\n  {inv_path}\n  {ds_path}\n  {report_path}")

    if to_s3:
        client = s3_client()
        for local, name in [
            (inv_path, "inventory.parquet"),
            (ds_path, "dataset_profile.parquet"),
            (str(report_path), "EDA_datasets.md"),
        ]:
            client.upload_file(str(local), bucket, f"gold/_eda/{name}")
        if verbose:
            print(f"\nMirrored inventory + dataset profile to s3://{bucket}/gold/_eda/")

    return inv, ds


def main() -> None:
    ap = argparse.ArgumentParser(description="Inventory + profile the S3 medallion layers.")
    ap.add_argument(
        "--layers", nargs="+", default=list(LAYERS),
        help="Which top-level prefixes to scan (default: bronze silver gold).",
    )
    ap.add_argument("--bucket", default=None, help="Override S3_BUCKET.")
    ap.add_argument(
        "--to-s3", action="store_true",
        help="Also mirror summaries + report to s3://<bucket>/gold/_eda/.",
    )
    ap.add_argument(
        "--deep", action="store_true",
        help="Per-file null/date-range profiling (SLOW + memory-heavy over many "
             "part-files). Default is the fast dataset-grain metadata profile.",
    )
    args = ap.parse_args()
    layers = tuple(args.layers)
    if args.deep:
        run(layers=layers, bucket=args.bucket, to_s3=args.to_s3)
    else:
        run_fast(layers=layers, bucket=args.bucket, to_s3=args.to_s3)


if __name__ == "__main__":
    main()
