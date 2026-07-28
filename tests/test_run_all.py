import types

import polars as pl
from eia_pipeline import run_all


def test_land_table_writes_gold_parquet_locally(tmp_path, monkeypatch):
    # `settings` is a frozen dataclass — don't mutate its attrs. Swap the whole `settings`
    # object that `io` references for a lightweight stand-in pointing data_dir at tmp_path.
    from eia_pipeline import io as io_mod
    monkeypatch.setattr(io_mod, "settings", types.SimpleNamespace(data_dir=tmp_path))

    df = pl.DataFrame({"county": ["SAN FRANCISCO"], "value": [123]})
    path = run_all.land_table("cdtfa_food_services", df, to_s3=False)

    written = tmp_path / "gold" / "cdtfa_food_services.parquet"
    assert written.exists()
    assert str(written) == path
    assert pl.read_parquet(written).equals(df)


def test_build_mlb_concats_seasons(monkeypatch):
    from eia_pipeline import run_all

    def fake_schedule(season):
        return pl.DataFrame({"season": [season], "game_pk": [season * 10]})

    monkeypatch.setattr(run_all.mlb, "fetch_home_schedule", fake_schedule)
    out = run_all.build_mlb([2023, 2024])
    assert out.height == 2
    assert sorted(out["season"].to_list()) == [2023, 2024]


def test_main_lands_three_tables(monkeypatch):
    from eia_pipeline import run_all

    landed = []
    monkeypatch.setattr(run_all, "build_cdtfa", lambda: pl.DataFrame({"a": [1]}))
    monkeypatch.setattr(run_all, "build_oi", lambda: pl.DataFrame({"b": [2]}))
    monkeypatch.setattr(run_all, "build_mlb", lambda seasons: pl.DataFrame({"c": [3]}))

    def fake_land(name, df, to_s3):
        landed.append((name, to_s3))
        return f"path/{name}"

    monkeypatch.setattr(run_all, "land_table", fake_land)

    out = run_all.main(["--seasons", "2024"])   # no --to-s3 => local only
    assert [n for n, _ in landed] == ["cdtfa_food_services", "oi_daily_spend", "mlb_home_schedule"]
    assert all(to_s3 is False for _, to_s3 in landed)
    assert len(out) == 3
