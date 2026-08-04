"""Smoke tests for the report figures.

We render for real rather than mocking matplotlib. The failure mode worth catching is a
figure whose input file moved or whose schema changed, and only an actual render catches
that. We skip rather than fail when an input is missing, so the suite still passes on a
fresh clone that has not pulled the panel down yet.

Output goes to tmp_path and never to docs/reports/figures, because running the tests must
not quietly rewrite the checked-in PNGs.
"""
from __future__ import annotations

import pytest

from eia_pipeline.eda import charts

# Each figure and the file it cannot be drawn without.
INPUTS = {
    "panel_coverage": "bronze_sf/model_hour.parquet",
    "bart_calibration": "gold/bart_attendance_calib_2024.parquet",
    "diurnal_profile": "bronze_sf/model_hour.parquet",
    "covariate_balance": "bronze_sf/model_hour.parquet",
    "distance_decay": "bronze_sf/tier3_crossover.json",
    "feature_ablation": None,  # numbers are literals from the model report
    "edge_ablation_seeds": "bronze_sf/tier2_ablation_seeds.json",
    "dollars_by_band": None,  # numbers are literals from the model report
    "target_distribution": "bronze_sf/model_hour.parquet",
    "missingness": "bronze_sf/model_hour.parquet",
    "covariate_correlation": "bronze_sf/model_hour.parquet",
    "split_drift": "bronze_sf/model_hour.parquet",
}


def test_every_figure_is_registered_and_tested():
    assert set(charts.FIGURES) == set(INPUTS)


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_figure_renders(name, tmp_path, monkeypatch):
    needed = INPUTS[name]
    if needed and not (charts.DATA / needed).exists():
        pytest.skip(f"{needed} not present locally")
    monkeypatch.setattr(charts, "FIG_DIR", tmp_path)
    charts._setup()
    out = charts.FIGURES[name]()
    assert out.parent == tmp_path
    # A blank or half-drawn canvas still writes a valid PNG, so we check it has heft.
    assert out.stat().st_size > 20_000


def test_main_rejects_an_unknown_figure():
    with pytest.raises(SystemExit):
        charts.main(["no_such_figure"])
