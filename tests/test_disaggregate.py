"""Deterministic tests for temporal disaggregation — the sum-back property (no network)."""
import numpy as np

from eia_pipeline.nowcast.disaggregate import build_agg_matrix, chow_lin, denton_proportional


def _irregular_setup():
    # 3 quarters of irregular length (90, 91, 92 days) -> the case off-the-shelf tools break on
    sizes = [90, 91, 92]
    group_ids = np.repeat([0, 1, 2], sizes)
    C = build_agg_matrix(group_ids, conversion="sum")
    rng = np.random.default_rng(0)
    indicator = 1000 + 200 * rng.random(sum(sizes))  # strictly positive
    y_low = np.array([1_072_906_635.0, 1_194_561_464.0, 1_180_687_372.0])  # real-ish SF $ magnitudes
    return y_low, indicator, C, group_ids


def test_agg_matrix_row_sums_are_group_sizes():
    _, _, C, _ = _irregular_setup()
    assert C.shape == (3, 273)
    assert list(C.sum(axis=1).astype(int)) == [90, 91, 92]


def test_denton_sums_back_exactly_and_no_negatives():
    y_low, indicator, C, _ = _irregular_setup()
    x = denton_proportional(y_low, indicator, C)
    got = C @ x
    assert np.max(np.abs((got - y_low) / y_low)) < 1e-9   # exact reconciliation
    assert x.min() > 0                                     # positive indicator -> positive $


def test_chow_lin_sums_back_exactly():
    y_low, indicator, C, _ = _irregular_setup()
    x, se, rho = chow_lin(y_low, indicator, C, return_se=True)
    got = C @ x
    assert np.max(np.abs((got - y_low) / y_low)) < 1e-9
    assert se.shape == x.shape
    assert 0.0 <= rho <= 0.98
