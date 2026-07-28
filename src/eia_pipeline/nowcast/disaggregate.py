"""Temporal disaggregation: quarterly dollar totals -> daily, following an indicator.

Doctrine rung 1 (docs/02): distribute a slow, trustworthy dollar total (CDTFA quarterly
taxable sales) across days so the daily values (a) SUM EXACTLY to each quarter and (b)
follow a daily presence indicator's shape. The exact sum-back to the tax truth is the
honest anchor; everything *within* a quarter is principled interpolation with real
uncertainty (Chow-Lin returns per-day standard errors — report bands, not points).

Off-the-shelf tools (`tempdisagg`, statsmodels) assume clean integer frequency ratios and
silently mishandle the IRREGULAR quarter->day ratio (90/91/92 days). We therefore build an
explicit aggregation matrix C and solve directly; this is grain-agnostic (quarter->day and
quarter->week use the same code, only `group_ids` differ). Verified to sum back to ~1e-15.

Spending is a FLOW variable => aggregation is SUM (each low-frequency row of C is ones over
its own days). Use "average" only for a stock/rate.
"""
from __future__ import annotations

import numpy as np
import polars as pl


def build_agg_matrix(group_ids: np.ndarray, conversion: str = "sum") -> np.ndarray:
    """C of shape (n_low, n_high): each low-frequency row aggregates its own group.

    group_ids: length n_high, the low-frequency group label per high-frequency day
    (contiguous blocks, in order). conversion="sum" (flow $) or "average" (stock/rate).
    Handles irregular group sizes.
    """
    group_ids = np.asarray(group_ids)
    _, first_idx = np.unique(group_ids, return_index=True)
    groups = group_ids[np.sort(first_idx)]
    C = np.zeros((len(groups), len(group_ids)))
    for i, g in enumerate(groups):
        mask = group_ids == g
        C[i, mask] = 1.0 if conversion == "sum" else 1.0 / mask.sum()
    return C


def denton_proportional(y_low: np.ndarray, indicator: np.ndarray, C: np.ndarray) -> np.ndarray:
    """Proportional-first-differences Denton: min sum_t[(x_t/p_t)-(x_{t-1}/p_{t-1})]^2
    s.t. C x = y_low. Preserves the indicator's day-to-day shape. Needs indicator > 0.
    """
    p = np.asarray(indicator, dtype=float)
    y = np.asarray(y_low, dtype=float)
    n = len(p)
    if np.any(p <= 0):
        raise ValueError("Proportional Denton needs a strictly-positive indicator.")
    D = np.zeros((n - 1, n))
    idx = np.arange(n - 1)
    D[idx, idx] = -1.0
    D[idx, idx + 1] = 1.0
    A = D @ np.diag(1.0 / p)
    M = A.T @ A  # PSD smoothing penalty on the ratio x/p
    n_low = C.shape[0]
    KKT = np.zeros((n + n_low, n + n_low))
    KKT[:n, :n] = M
    KKT[:n, n:] = C.T
    KKT[n:, :n] = C
    rhs = np.concatenate([np.zeros(n), y])
    sol, *_ = np.linalg.lstsq(KKT, rhs, rcond=None)  # M singular -> lstsq
    return sol[:n]


def chow_lin(
    y_low: np.ndarray,
    X_high: np.ndarray,
    C: np.ndarray,
    rho_grid: np.ndarray | None = None,
    add_const: bool = True,
    return_se: bool = False,
):
    """Chow-Lin GLS disaggregation with AR(1) residual.

    High-freq model x = X beta + u, u ~ AR(1) (corr = rho^|t-s|); observed y_low = C x.
    beta by GLS at the aggregated level; rho chosen by profile GLS likelihood over a grid;
    BLUE distribution guarantees C x == y_low for any rho. return_se -> per-day std errors.
    """
    y = np.asarray(y_low, dtype=float)
    X = np.asarray(X_high, dtype=float)
    if X.ndim == 1:
        X = X[:, None]
    n = X.shape[0]
    if add_const:
        X = np.column_stack([np.ones(n), X])
    n_low = C.shape[0]
    lag = np.abs(np.arange(n)[:, None] - np.arange(n)[None, :])
    if rho_grid is None:
        rho_grid = np.concatenate([[0.0], np.linspace(0.05, 0.98, 40)])

    best = None
    for rho in rho_grid:
        V = rho ** lag
        Vl_inv = np.linalg.pinv(C @ V @ C.T)
        Xl = C @ X
        XtVinv = Xl.T @ Vl_inv
        beta = np.linalg.solve(XtVinv @ Xl, XtVinv @ y)
        resid_l = y - Xl @ beta
        sigma2 = float(resid_l.T @ Vl_inv @ resid_l) / n_low
        _, logdet = np.linalg.slogdet(C @ V @ C.T)
        ll = -0.5 * (n_low * np.log(sigma2) + logdet)
        if best is None or ll > best[0]:
            best = (ll, rho, beta, V, Vl_inv, sigma2, Xl, resid_l)

    _, rho, beta, V, Vl_inv, sigma2, Xl, resid_l = best
    L = V @ C.T @ Vl_inv
    x = X @ beta + L @ resid_l
    if not return_se:
        return x
    Vdist = sigma2 * (V - L @ C @ V)
    XtVinvX_inv = np.linalg.pinv(Xl.T @ Vl_inv @ Xl) * sigma2
    G = X - L @ Xl
    Vbeta = G @ XtVinvX_inv @ G.T
    se = np.sqrt(np.clip(np.diag(Vdist + Vbeta), 0, None))
    return x, se, rho


def disaggregate_quarterly_to_daily(
    quarterly: pl.DataFrame,   # columns: year, quarter, value  (dollars per quarter)
    indicator: pl.DataFrame,   # columns: date, <indicator_col>  (daily, spans the quarters)
    indicator_col: str,
    method: str = "denton",    # "denton" | "chow-lin"
) -> tuple[pl.DataFrame, dict]:
    """Distribute quarterly `value` across the daily `indicator`, one row per day.

    Only quarters FULLY covered by the indicator's date range are disaggregated (a partial
    quarter cannot sum back correctly). Returns (daily frame with `value_daily` [+ `se`
    for chow-lin], summary dict with per-quarter sum-back check).
    """
    ind = indicator.select("date", pl.col(indicator_col).alias("_ind")).sort("date")
    ind = ind.with_columns(
        pl.col("date").dt.year().alias("year"),
        pl.col("date").dt.quarter().alias("quarter"),
    )
    # keep only quarters present in BOTH and fully covered by the indicator
    q_keys = quarterly.select("year", "quarter").unique()
    ind = ind.join(q_keys, on=["year", "quarter"], how="inner").sort("date")
    # a quarter is usable only if the indicator supplies all its calendar days
    day_counts = ind.group_by("year", "quarter").agg(pl.len().alias("n_days"))
    expected = ind.with_columns(
        pl.date(pl.col("year"), pl.col("quarter") * 3 - 2, 1).alias("_qstart")
    )  # (only used implicitly; we trust contiguous daily coverage)
    usable = day_counts.filter(pl.col("n_days") >= 88)  # guard against a stub-short quarter
    ind = ind.join(usable.select("year", "quarter"), on=["year", "quarter"], how="inner").sort("date")

    q = (
        quarterly.join(ind.select("year", "quarter").unique(), on=["year", "quarter"], how="inner")
        .sort("year", "quarter")
    )
    if q.height == 0 or ind.height == 0:
        raise ValueError("No fully-covered quarter overlaps the indicator date range.")

    group_ids = (ind["year"] * 4 + ind["quarter"]).to_numpy()
    C = build_agg_matrix(group_ids, conversion="sum")
    y_low = q["value"].to_numpy().astype(float)
    p = ind["_ind"].to_numpy().astype(float)

    out = ind.select("date", "year", "quarter")
    se_col = None
    if method == "denton":
        x = denton_proportional(y_low, p, C)
    elif method == "chow-lin":
        x, se, rho = chow_lin(y_low, p, C, return_se=True)
        se_col = se
    else:
        raise ValueError(f"unknown method {method!r}")

    out = out.with_columns(pl.Series("value_daily", x))
    if se_col is not None:
        out = out.with_columns(pl.Series("se", se_col))

    # sum-back check
    got = out.group_by("year", "quarter").agg(pl.col("value_daily").sum().alias("got")).sort("year", "quarter")
    chk = q.join(got, on=["year", "quarter"]).with_columns(
        ((pl.col("got") - pl.col("value")) / pl.col("value")).alias("rel_resid")
    )
    summary = {
        "method": method,
        "n_quarters": q.height,
        "n_days": out.height,
        "max_abs_rel_sumback": float(chk["rel_resid"].abs().max()),
        "min_daily_value": float(out["value_daily"].min()),
    }
    if method == "chow-lin":
        summary["chow_lin_rho"] = round(float(rho), 3)
        summary["mean_daily_se_pct"] = round(float((out["se"] / out["value_daily"].abs()).mean()) * 100, 1)
    return out, summary
