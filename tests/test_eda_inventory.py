"""Tests for the medallion EDA inventory/profile — pure pieces + DuckDB profiling
against a local Parquet fixture (no live S3)."""
from __future__ import annotations

import datetime as dt

import duckdb
import polars as pl

from eia_pipeline.eda import inventory as inv


def _fixture(tmp_path):
    df = pl.DataFrame(
        {
            "date": [dt.date(2024, 4, 1), dt.date(2024, 4, 2), dt.date(2024, 4, 3)],
            "station": ["EMBR", "MONT", None],
            "trip_count": [10, 20, 30],
        }
    )
    p = tmp_path / "sample.parquet"
    df.write_parquet(p)
    return p


def test_profile_parquet_local(tmp_path):
    p = _fixture(tmp_path)
    out = inv.profile_parquet(str(p), duckdb.connect())
    assert out["error"] is None
    assert out["n_rows"] == 3
    assert out["n_cols"] == 3
    # date column detected by DATE type, range correct
    assert out["date_col"] == "date"
    assert out["date_min"] == "2024-04-01"
    assert out["date_max"] == "2024-04-03"
    # null rate on `station` (1 of 3 null)
    station = next(c for c in out["columns"] if c["column"] == "station")
    assert station["n_null"] == 1
    assert abs(station["null_rate"] - 0.3333) < 1e-3


def test_profile_parquet_bad_uri_soft_fails():
    out = inv.profile_parquet("s3://nope/does-not-exist.parquet", duckdb.connect())
    assert out["error"] is not None  # captured, not raised
    assert out["n_rows"] is None


def test_pick_date_column_prefers_type_then_name():
    # typed DATE wins even when named oddly
    assert inv._pick_date_column(["x", "y"], ["DATE", "VARCHAR"]) == "x"
    # falls back to a name hint when no date-typed column
    assert inv._pick_date_column(["game_day", "n"], ["VARCHAR", "BIGINT"]) == "game_day"
    # none when neither
    assert inv._pick_date_column(["a", "b"], ["BIGINT", "VARCHAR"]) is None


def test_render_report_groups_by_layer_and_handles_empty():
    inv_df = pl.DataFrame(
        {
            "layer": ["gold"],
            "key": ["gold/t.parquet"],
            "filename": ["t.parquet"],
            "ext": ["parquet"],
            "size_bytes": [2048],
            "size_mb": [0.002],
            "last_modified": [dt.datetime(2024, 4, 1)],
        }
    )
    table_df = pl.DataFrame(
        {
            "layer": ["gold"], "key": ["gold/t.parquet"], "size_mb": [0.002],
            "n_rows": [3], "n_cols": [3], "date_col": ["date"],
            "date_min": ["2024-04-01"], "date_max": ["2024-04-03"],
            "status": ["ok"], "note": [""],
        }
    )
    md = inv.render_report(inv_df, table_df, bucket="test-bucket")
    assert "## gold" in md
    assert "## bronze" in md and "_no objects_" in md  # empty layers reported
    assert "gold/t.parquet" in md
    assert "2024-04-01 → 2024-04-03" in md


def test_profile_all_empty_inventory_returns_typed_empties():
    empty_inv = pl.DataFrame(
        schema={"layer": pl.Utf8, "key": pl.Utf8, "ext": pl.Utf8, "size_mb": pl.Float64}
    )
    table_df, col_df = inv.profile_all(empty_inv, bucket="b")
    assert table_df.is_empty() and col_df.is_empty()
    assert table_df.schema["status"] == pl.Utf8  # schema present despite no rows


def test_dataset_key_groups_partfiles_but_not_singles():
    assert inv._dataset_key("silver/panel_ring_day.parquet") == "silver/panel_ring_day.parquet"
    assert inv._dataset_key("silver/advan_hourly/2026-02-02.parquet") == "silver/advan_hourly"
    assert inv._dataset_key("bronze/advan_weekly_patterns/2020-01-06--x.parquet") == "bronze/advan_weekly_patterns"


def test_profile_fast_rolls_up_datasets(tmp_path, monkeypatch):
    # two part-files for one dataset (3 + 2 rows) + one standalone table (4 rows)
    (tmp_path / "silver" / "advan_hourly").mkdir(parents=True)
    pl.DataFrame({"a": [1, 2, 3]}).write_parquet(tmp_path / "silver" / "advan_hourly" / "w1.parquet")
    pl.DataFrame({"a": [4, 5]}).write_parquet(tmp_path / "silver" / "advan_hourly" / "w2.parquet")
    (tmp_path / "silver").mkdir(exist_ok=True)
    pl.DataFrame({"x": [1, 2, 3, 4], "y": [0, 0, 0, 0]}).write_parquet(tmp_path / "silver" / "panel.parquet")

    inv_df = pl.DataFrame(
        {
            "layer": ["silver", "silver", "silver"],
            "key": ["silver/advan_hourly/w1.parquet", "silver/advan_hourly/w2.parquet", "silver/panel.parquet"],
            "ext": ["parquet", "parquet", "parquet"],
            "size_mb": [0.1, 0.1, 0.2],
        }
    )
    # redirect s3:// URIs to the local fixtures; use a plain (non-S3) duckdb connection
    monkeypatch.setattr(inv, "_uri", lambda bucket, key: str(tmp_path / key))
    monkeypatch.setattr(inv, "duckdb_s3", lambda: duckdb.connect())

    ds = inv.profile_fast(inv_df, bucket="b")
    assert ds.height == 2  # advan_hourly dataset + panel table
    ah = ds.filter(pl.col("dataset") == "silver/advan_hourly").to_dicts()[0]
    assert ah["n_files"] == 2 and ah["n_rows"] == 5  # rows summed across part-files
    panel = ds.filter(pl.col("dataset") == "silver/panel.parquet").to_dicts()[0]
    assert panel["n_files"] == 1 and panel["n_rows"] == 4 and panel["n_cols"] == 2


def test_profile_fast_empty_is_typed():
    empty = pl.DataFrame(schema={"layer": pl.Utf8, "key": pl.Utf8, "ext": pl.Utf8, "size_mb": pl.Float64})
    ds = inv.profile_fast(empty, bucket="b")
    assert ds.is_empty() and ds.schema["n_rows"] == pl.Int64


def test_profile_all_skips_non_parquet(tmp_path, monkeypatch):
    # A CSV object should be listed but skipped (status='skipped'), no DuckDB call.
    inv_df = pl.DataFrame(
        {"layer": ["bronze"], "key": ["bronze/notes.csv"], "ext": ["csv"], "size_mb": [0.01]}
    )
    called = {"n": 0}
    monkeypatch.setattr(inv, "duckdb_s3", lambda: called.__setitem__("n", called["n"] + 1))
    table_df, col_df = inv.profile_all(inv_df, bucket="b")
    assert table_df["status"][0] == "skipped"
    assert col_df.is_empty()
