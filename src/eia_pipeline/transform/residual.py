"""Weather-adjusted wastewater flow residual (Task 01 core).

Model daily flow on weather + calendar; the RESIDUAL (flow above weather-predicted)
is the candidate crowd anomaly. This is the crude, operational path (decisions D5) —
NOT the pathogen-dilution method. Keep it honest: weather dominates flow; residual =
net imported visitors, not gross attendance; daily is the granularity floor.
"""
from __future__ import annotations

import polars as pl


def antecedent_precip_index(precip: pl.Series, k: float = 0.9) -> pl.Series:
    """Decaying weighted sum of prior-day precipitation: API_t = precip_t + k*API_{t-1}.
    Encodes how long the sewershed 'remembers' rain (k ~ 0.85-0.95/day).
    """
    raise NotImplementedError("Implement the recursive decay; see docs/decisions D5.")


def flow_residual(joined: pl.DataFrame) -> pl.DataFrame:
    """Given a flow+weather daily frame, fit flow ~ precip + antecedent soil moisture +
    ET + temp + day-of-week + month, and return the input with a `flow_resid` column.

    Start simple (OLS/statsmodels). The principled version is dynamic regression with a
    distributed-lag event term + ARIMA errors, hierarchical across facilities (docs/02) —
    but do NOT put lagged flow on the RHS as an AR feature: it would launder away the
    day+1 event spillover. Handle autocorrelation via ARIMA errors instead.
    """
    raise NotImplementedError("Fit baseline regression; return residual. See tasks/01.")
