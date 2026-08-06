"""Invariants for the serving path. Only the things that fail SILENTLY.

Same discipline as test_citywide_invariants.py: we do not assert effect sizes or
MAEs, because those move with the data. We assert the structural properties that,
if broken, produce output that looks entirely plausible and that reading the code
would not catch.

The ones with no data dependency run anywhere. The ones that need the rebuilt
panel or the fitted booster skip rather than fail, so a fresh clone still passes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from eia_pipeline.serve import featurespec as fs
from eia_pipeline.settings import settings

BASE = settings.data_dir / "bronze_sf"
ARTIFACTS = settings.data_dir / "serve_artifacts"


def _need(path: Path):
    if not path.exists():
        pytest.skip(f"{path.name} not present locally")


@pytest.fixture(scope="module")
def booster_cats():
    _need(BASE / "served_model.txt")
    from eia_pipeline.serve.fitmodel import load_cached

    return load_cached()


@pytest.fixture(scope="module")
def sample():
    """One full week: every one of the 452 cells appears, so _xy sees them all."""
    _need(BASE / "model_hour.parquet")
    from eia_pipeline.nowcast.models import tier1_gbm as t

    df = t.load()
    return df.filter(
        (pl.col("date") >= pl.date(2024, 7, 1)) & (pl.col("date") <= pl.date(2024, 7, 7))
    )


# ------------------------------------------------- the feature-matrix contract


def test_build_x_reproduces_the_training_matrix(booster_cats, sample):
    """build_X must equal _xy: values, dtypes and column order.

    If it drifts, the endpoint answers with a different matrix than the model was
    fitted on and nothing raises.
    """
    from eia_pipeline.nowcast.models import tier1_gbm as t

    _, cats = booster_cats
    X_train, _ = t._xy(sample)
    X_serve = fs.build_X({f: sample[f].to_numpy() for f in fs.FEATURES}, cats)

    assert list(X_serve.columns) == fs.FEATURES == list(X_train.columns)
    for c in fs.FEATURES:
        if c in fs.CATEGORICAL:
            assert list(X_serve[c].cat.categories) == list(X_train[c].cat.categories)
            assert np.array_equal(X_serve[c].cat.codes.to_numpy(),
                                  X_train[c].cat.codes.to_numpy())
        else:
            assert np.allclose(X_serve[c].to_numpy(), X_train[c].to_numpy(),
                               equal_nan=True)


def test_serve_path_predicts_identically_to_training(booster_cats, sample):
    from eia_pipeline.nowcast.models import tier1_gbm as t

    booster, cats = booster_cats
    X_train, _ = t._xy(sample)
    X_serve = fs.build_X({f: sample[f].to_numpy() for f in fs.FEATURES}, cats)
    assert np.array_equal(booster.predict(X_train), fs.predict_cf(booster, X_serve))


def test_raw_numpy_input_gives_a_different_answer(booster_cats, sample):
    """THE TRAP, written down.

    Passing a numpy array skips pandas' categorical handling, so unit_code's raw
    values 1..452 go in where 0-based codes belong and every cell is scored as its
    lexicographic neighbour. No exception is raised. This test exists so that if
    anyone ever "simplifies" build_X into a .to_numpy(), the suite says why not.
    """
    booster, cats = booster_cats
    X = fs.build_X({f: sample[f].to_numpy() for f in fs.FEATURES}, cats)
    correct = fs.predict_cf(booster, X)
    naive = booster.predict(X.to_numpy().astype(float))
    assert not np.allclose(correct, naive)
    # not a rounding difference: it is wrong nearly everywhere, and largely so
    assert (np.abs(correct - naive) > 0.01).mean() > 0.5


def test_permuted_columns_raise(booster_cats, sample):
    booster, cats = booster_cats
    X = fs.build_X({f: sample[f].to_numpy() for f in fs.FEATURES}, cats)
    swapped = X[[fs.FEATURES[1], fs.FEATURES[0]] + fs.FEATURES[2:]]
    with pytest.raises(ValueError):
        fs.predict_cf(booster, swapped)


def test_unknown_unit_code_raises(booster_cats, sample):
    _, cats = booster_cats
    cols = {f: sample[f].to_numpy() for f in fs.FEATURES}
    cols["unit_code"] = np.full(sample.height, 9999)
    with pytest.raises(ValueError):
        fs.build_X(cols, cats)


def test_unit_code_is_a_one_based_lexicographic_dense_rank(booster_cats):
    """One cell crossing the >=10-POI threshold renumbers every cell above it."""
    _need(BASE / "cell_dim.parquet")
    booster, cats = booster_cats
    units = sorted(pl.read_parquet(BASE / "cell_dim.parquet")["unit_id"].to_list())
    assert list(cats) == list(range(1, len(units) + 1))
    assert units[0] < units[1]                       # plain string ordering


# ------------------------------------------------------------- the effect layer


def _eff():
    p = BASE / "effects.json"
    _need(p)
    return json.loads(p.read_text())


def test_composition_multiplies_levels_not_logs():
    """Adding in log space agrees at large counts and diverges where counts are
    small, which is most of this panel. The error is invisible in a chart."""
    cf_log = np.log1p(np.array([2.0]))
    eff_log = np.log(np.array([1.5]))
    level, extra = fs.compose(cf_log, eff_log)
    assert level[0] == pytest.approx(3.0)
    assert extra[0] == pytest.approx(1.0)
    assert level[0] != pytest.approx(np.expm1(cf_log[0] + eff_log[0]))


def test_suppressed_band_gets_exactly_no_lift():
    """Zeroing a band's SHIFT is not enough: the decay's asymptote c still leaks
    lift into it. Suppression has to happen at the multiplier."""
    eff = _eff()
    sup = [b for b in eff["bands"] if not b["significant"]]
    if not sup:
        pytest.skip("no suppressed band in this estimate")
    b = sup[0]
    d = np.array([float(b["inner_m"]) + 1000.0])
    out = fs.effect_log(d, [b["id"]], np.array([eff["response"]["center"]]),
                        np.array([0.0]), eff)
    assert out[0] == 0.0


def test_effect_is_not_increasing_with_distance_off_centre():
    """The per-band responses are fitted independently, so without the monotone
    cap an outer band overtakes an inner one once attendance moves off centre.
    Measured: at 35,060 attendance the 2.5-5km band reached +2.57% against
    1-2.5km at +1.97%.

    Asserted on the band TOTALS, which is the quantity the cap acts on. Not on
    the per-row offsets: each band's calibration shift is relative to its own
    distance range, so the shifts are not ordered and never were meant to be.
    """
    eff = _eff()
    for z in (-2.0, -1.0, 0.0, 1.0, 2.0):
        for nt in (0.0, 1.0):
            tot = [v for v in fs.band_totals(eff, z, nt).values() if v is not None]
            assert all(tot[i] >= tot[i + 1] - 1e-12 for i in range(len(tot) - 1)), (
                f"band totals increase with distance at z={z}, night={nt}: {tot}")


def test_monotone_cap_is_inactive_at_the_calibration_point():
    """The cap must not touch the published numbers. At mean attendance and a day
    game the totals are already ordered, so every one equals its own did_log."""
    eff = _eff()
    tot = fs.band_totals(eff, 0.0, 0.0)
    for b in eff["bands"]:
        if b["significant"]:
            assert tot[b["id"]] == pytest.approx(float(b["did_log"]), abs=1e-12)


def test_calibration_reproduces_the_band_did_at_mean_attendance():
    """The smooth decay is an interpolation of the published DiD, not a competing
    estimate of it. If this drifts, the site's bars stop matching the report."""
    _need(ARTIFACTS / "cells.npz")
    eff = _eff()
    cells = np.load(ARTIFACTS / "cells.npz")
    dist = cells["dist_venue_m"]
    # weight by n_poi as a stand-in for activity; the calibration identity holds
    # for any positive weighting only at the weights it was fitted with, so this
    # asserts the WEAK form: sign and rough magnitude, exactly reproduced in the
    # dedicated build-time check.
    for b in eff["bands"]:
        if not b["significant"]:
            continue
        m = np.array([fs.band_of(float(d), eff["bands"]) == b["id"] for d in dist])
        if not m.any():
            continue
        el = fs.effect_log(dist[m], [b["id"]] * int(m.sum()),
                           np.full(int(m.sum()), eff["response"]["center"]),
                           np.zeros(int(m.sum())), eff)
        agg = float(np.mean(np.exp(el)) - 1) * 100
        assert np.sign(agg) == np.sign(b["lift_pct"])
        assert abs(agg - b["lift_pct"]) < max(2.0, 0.25 * abs(b["lift_pct"]))


# ------------------------------------------------------- the serve tables


def test_serve_baselines_are_unchanged_on_the_training_overlap():
    """The training window must reproduce bit for bit after the 2026 extension."""
    for p in (BASE / "rolling_baseline.parquet", BASE / "rolling_baseline_serve.parquet"):
        _need(p)
    import duckdb

    lo, hi = fs.EVENING
    d = duckdb.connect().execute(
        f"""
        SELECT max(abs(coalesce(a.base_k2,-9) - coalesce(b.base_k2,-9))),
               max(abs(coalesce(a.base_cap120,-9) - coalesce(b.base_cap120,-9))),
               count(*)
        FROM read_parquet('{BASE}/rolling_baseline.parquet') a
        JOIN read_parquet('{BASE}/rolling_baseline_serve.parquet') b
          USING (unit_id, date, hour)
        WHERE a.date <= DATE '2025-12-31' AND a.hour BETWEEN {lo} AND {hi}
        """
    ).fetchone()
    assert d[2] > 0, "no overlap rows joined; the keys do not line up"
    assert d[0] < 1e-12 and d[1] < 1e-12


def test_unobserved_future_rows_never_enter_the_control_windows():
    """The single most important line in spine_2026.

    A future non-game date has person_hours = 0 and clean_control_strict = TRUE.
    Without `AND observed` on the control CTE those zeros join the trailing means
    and flatten the baseline for every date after them. Built here on a tiny
    synthetic panel so the test does not need the real data.
    """
    import duckdb

    import datetime as dt

    start = dt.date(2026, 1, 6)              # a Tuesday; dow is constant by design
    rows = [("u1", start + dt.timedelta(days=7 * i), 20, 2, 5.0, True, True)
            for i in range(8)]               # 8 same-weekday observed controls
    rows += [("u1", start + dt.timedelta(days=7 * (8 + i)), 20, 2, 0.0, True, False)
             for i in range(3)]              # then unobserved future zeros
    con = duckdb.connect()
    con.execute("CREATE TABLE panel (unit_id VARCHAR, date DATE, hour INT, dow INT, "
                "y DOUBLE, clean_control_strict BOOLEAN, observed BOOLEAN)")
    con.executemany("INSERT INTO panel VALUES (?,?,?,?,?,?,?)", rows)

    def last_base(where: str) -> float:
        return con.execute(f"""
            WITH ctl AS (
                SELECT unit_id, date, hour, dow,
                       avg(y) OVER (PARTITION BY unit_id, dow, hour ORDER BY date
                                    ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS base_k2
                FROM panel WHERE {where})
            SELECT c.base_k2 FROM panel p
            ASOF LEFT JOIN ctl c ON p.unit_id=c.unit_id AND p.hour=c.hour
                 AND p.dow=c.dow AND p.date > c.date
            ORDER BY p.date DESC LIMIT 1""").fetchone()[0]

    guarded = last_base("clean_control_strict AND observed")
    unguarded = last_base("clean_control_strict")
    assert guarded == pytest.approx(5.0), "observed baseline should be untouched"
    assert unguarded < guarded, "unguarded build must be dragged down by the zeros"


def test_evening_window_is_the_only_supported_window():
    assert fs.EVENING == (16, 23)


# ------------------------------------------------------------ the packaged copy


def test_packaged_featurespec_is_byte_identical():
    tar = settings.data_dir / "dist" / "model.tar.gz"
    _need(tar)
    import tarfile

    with tarfile.open(tar) as t:
        packaged = t.extractfile("code/featurespec.py").read()
    local = Path(fs.__file__).read_bytes()
    assert hashlib.sha256(packaged).hexdigest() == hashlib.sha256(local).hexdigest()


def test_manifest_records_the_row_index_contract():
    _need(ARTIFACTS / "manifest.json")
    man = json.loads((ARTIFACTS / "manifest.json").read_text())
    assert man["features"] == fs.FEATURES
    assert man["n_rows"] == man["n_cells"] * man["n_dates"] * (
        man["evening_hours"][1] - man["evening_hours"][0] + 1)


def test_model_geometry_matches_the_website_cells():
    """PLUG-IN-ENDPOINT.md step 5, enforced. A mismatch renders a blank map."""
    ids_p = ARTIFACTS / "unit_ids.json"
    site_p = Path(__file__).resolve().parents[1] / "website/src/data/cells.json"
    _need(ids_p)
    _need(site_p)
    model = set(json.loads(ids_p.read_text()))
    site = {c["id"] for c in json.loads(site_p.read_text())["cells"]}
    assert model == site, (
        f"{len(model - site)} model-only, {len(site - model)} site-only cells; "
        "regenerate website/scripts/build-cells.py"
    )
