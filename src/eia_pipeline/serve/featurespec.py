"""The one place the Tier 1 feature matrix is assembled.

This file is copied VERBATIM into the serving tarball's code/ directory, and
package.py records its sha256 in the manifest so a test can prove the two copies
are byte-identical. That is why it imports numpy and pandas and nothing else: no
polars, no duckdb, no lightgbm, no eia_pipeline. Keep it that way, and keep the
syntax Python 3.8-compatible, because the SageMaker container may not be 3.11.

WHY THIS EXISTS: three ways to get a confidently wrong answer

`tier1_gbm._xy()` does `X['unit_code'].astype('category')` on an Int32 column
holding 1..452. pandas then assigns 0-based positional codes over the observed
category set, and LightGBM trains on those codes. So the model was fit on codes
0..451, NOT on the values 1..452. It stores the mapping as
`booster.pandas_categorical == [[1, 2, ..., 452]]` and can only reproduce it when
the predict input is a pandas DataFrame whose column is already `category`.

  1. Pass a numpy array. `_data_from_pandas` never runs, the raw values 1..452 go
     in as codes, and every cell is scored as its lexicographic neighbour. The
     output is plausible everywhere. SILENT.
  2. Pass a DataFrame with unit_code left as an int. LightGBM raises on the
     categorical-count mismatch. Loud, which is fine.
  3. Pass the 18 columns in the wrong order. `Booster.predict` defaults to
     `validate_features=False` and checks only the count. SILENT.

And a fourth, which is why `astype('category')` is not good enough here: pandas
derives the category set from the data in front of it. Call it on a subset that
happens to be missing a cell and you get 389 categories instead of 452, silently
renumbering every code above the gap. Measured, not hypothetical.

The fix is that `build_X` takes the category list FROM THE BOOSTER and never
re-derives it, and `predict_cf` passes `validate_features=True`. Together those
make modes 1, 3 and 4 impossible by construction rather than by discipline.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Order is load-bearing: `_xy` does `df.select(FEATURES)`, so this IS the column
# order the model was fit on. LightGBM checks the count and not the names unless
# asked, so a permutation here is failure mode 3 above.
FEATURES = [
    "unit_code",
    "base_k2", "base_k4", "base_cap120", "n_cap120",
    "hour", "dow", "month",
    "t_index",
    "n_poi_live",
    "n_poi", "food_share", "dist_venue_m", "bearing_venue_deg",
    "temp_hr", "prcp_hr", "wind_hr",
    "us_federal_holiday",
]
CATEGORICAL = ["unit_code"]

# Measured off `tier1_gbm._xy()`, not assumed. LightGBM widens everything to
# float64 internally, so these do not change a prediction; they exist so the
# golden fixture compares exactly and so a wrong-shaped input fails here rather
# than three layers down.
#
# n_cap120 is pinned to float64 DELIBERATELY, and it is the interesting one.
# It reaches pandas through a nullable polars column, so `to_pandas()` hands back
# int64 for a slice with no warm-up rows and float64 for one with them: the dtype
# depends on which rows you happened to select. Training saw float64 (the 1.1%
# warm-up NaNs sit in early 2023, inside the train split), but relying on that is
# relying on an accident. float64 is the wider type, always holds the value, and
# is the only choice that is the same for every slice. The base_* columns are
# float for the same reason; LightGBM handles their NaNs natively.
DTYPES = {
    "unit_code": "int32",
    "base_k2": "float64",
    "base_k4": "float64",
    "base_cap120": "float64",
    "n_cap120": "float64",
    "hour": "int8",
    "dow": "int64",
    "month": "int64",
    "t_index": "int64",
    "n_poi_live": "int64",
    "n_poi": "int64",
    "food_share": "float64",
    "dist_venue_m": "float64",
    "bearing_venue_deg": "float64",
    "temp_hr": "float64",
    "prcp_hr": "float64",
    "wind_hr": "float64",
    "us_federal_holiday": "int8",
}

TARGET = "person_hours"      # the model trains on log1p of this
EVENING = (16, 23)           # the only hours the effect layer supports


def build_X(cols, unit_code_categories):
    """Assemble the exact frame LightGBM was fit on.

    cols: mapping of feature name -> 1-D array-like, any order.
    unit_code_categories: MUST be `booster.pandas_categorical[0]`. Never re-derive
        it from the data; that is failure mode 4 in the module docstring.

    Raises rather than guessing on a missing feature or an unknown unit_code.
    """
    missing = [f for f in FEATURES if f not in cols]
    if missing:
        raise KeyError("missing features: %r" % (missing,))

    X = pd.DataFrame({f: np.asarray(cols[f]) for f in FEATURES}, columns=FEATURES)

    for f in FEATURES:
        if f not in CATEGORICAL:
            X[f] = X[f].astype(DTYPES[f])

    for c in CATEGORICAL:
        codes = X[c].astype(DTYPES[c])
        # Build the category index at the DECLARED dtype. A plain Python list
        # would give int64 categories where `_xy` produces int32. LightGBM matches
        # on value so the codes come out the same either way, but keeping the
        # dtype identical is what lets the golden fixture assert frame equality
        # instead of merely value equality.
        cats = pd.Index(np.asarray(unit_code_categories, dtype=DTYPES[c]))
        # Checked BEFORE constructing the Categorical. Relying on out-of-set
        # values becoming NaN is deprecated in pandas 3 and is slated to raise,
        # and a silently-NaN unit_code is precisely the failure this guard exists
        # to catch, so it must not depend on behaviour that is going away.
        unknown = np.setdiff1d(np.asarray(codes), np.asarray(cats))
        if unknown.size:
            raise ValueError(
                "unit_code values outside the trained category set: %r "
                "(the cell grid changed; the model cannot score these)"
                % (sorted(unknown.tolist())[:5],)
            )
        X[c] = pd.Categorical(codes, categories=cats)

    if list(X.columns) != FEATURES:            # belt and braces; pandas preserves it
        raise AssertionError("column order drifted during assembly")
    return X


def predict_cf(booster, X):
    """Counterfactual log1p(person_hours). expm1 it for a level.

    validate_features=True is the whole point: it turns a silent column
    permutation into an exception.
    """
    if list(X.columns) != FEATURES:
        raise ValueError(
            "column order is %r, expected %r" % (list(X.columns), FEATURES)
        )
    return booster.predict(X, validate_features=True)


# ---------------------------------------------------------------- effect layer
#
# The GBM never sees an event, so the game-day multiplier lives entirely here and
# is driven by effects.json. These two functions are the ONLY place it is applied;
# the handler, the local scorer and the tests all call them, so there is one
# implementation to get right rather than three to keep in sync.


def band_of(dist_m, bands):
    """First-match-wins on the upper bound. `bands` is effects.json's list."""
    for b in bands:
        hi = b.get("outer_m")
        if hi is None or dist_m < hi:
            return b["id"]
    return bands[-1]["id"]


def effect_log(dist_m, band_ids, attendance, is_night, eff):
    """Log-space game-day effect per row. Vectorized over rows.

    smooth decay + per-band calibration shift + attendance and day/night response.

    A band flagged `significant: false` returns EXACTLY ZERO, and that is not the
    same as letting its shift be zero. The decay has an asymptote c, so a cell 8 km
    out would otherwise still pick up c's worth of lift (measured: +0.68%) from a
    band whose own effect could not be told from zero. Suppression has to happen
    at the multiplier, not at the shift.

    Calibration holds AT MEAN ATTENDANCE: band_shifts pins the activity-weighted
    aggregate to the published DiD for a game at `response.center`. A busier or
    quieter game is then moved off that point on purpose; that is what the
    response is for.

    MONOTONE CAP. The per-band responses are fitted independently, so nothing in
    the fit stops an outer band from overtaking an inner one once attendance moves
    off centre. It happened on real data: at 35,060 attendance the 2.5-5km band
    came out at +2.57% against 1-2.5km at +1.97%, because the outer band carried a
    barely-significant attendance slope (CI lower bound 0.0002) and the inner one
    carried none. Those two effects are statistically indistinguishable to begin
    with (CIs +0.7 to +3.5 and +0.2 to +3.0), so the crossover is an artifact of
    fitting bands separately rather than a finding, and on the map it would read
    as the ripple growing with distance.

    So we impose the one structural property the distance-decay model actually
    asserts: the effect does not increase with distance. A running minimum over
    the band totals, in distance order. At mean attendance the totals are already
    ordered, so the cap is INACTIVE at the calibration point and the published
    band numbers are untouched; it only ever binds off-centre.
    """
    d = np.asarray(dist_m, dtype=float)
    att = np.asarray(attendance, dtype=float)
    night = np.asarray(is_night, dtype=float)
    resp = eff["response"]
    z = float(np.mean((att - resp["center"]) / (resp["scale"] or 1.0)))
    nt = float(np.mean(night))

    totals = band_totals(eff, z, nt)
    dec = eff["decay"]
    base = dec["A"] * np.exp(-d / dec["L"]) + dec["c"]
    out = np.zeros_like(base)
    ids = np.asarray(band_ids)
    for b in eff["bands"]:
        bid = b["id"]
        sel = ids == bid
        if not sel.any() or totals[bid] is None:
            continue                                  # suppressed: exactly 1x
        # the cap moves the band TOTAL, so the correction it implies is carried
        # into the per-row offset alongside the calibration shift
        raw = float(b.get("did_log", 0.0)) + _adj(eff, bid, z, nt)
        out[sel] = base[sel] + b.get("shift", 0.0) + _adj(eff, bid, z, nt) \
            + (totals[bid] - raw)
    return out


def _adj(eff, bid, z, nt):
    r = eff["response"]
    return r["beta"].get(bid, 0.0) * z + r["night"].get(bid, 0.0) * nt


def band_totals(eff, z, nt):
    """Per-band log effect after the monotone cap. None means suppressed.

    Separated out so the cap is directly testable: it is a running minimum over
    the bands in distance order, and asserting that on the offsets instead would
    be wrong, because each band's calibration shift is relative to its own
    distance range and the shifts are not themselves ordered.
    """
    out, running = {}, None
    for b in eff["bands"]:
        bid = b["id"]
        if not b.get("significant", True):
            out[bid] = None
            continue
        total = float(b.get("did_log", 0.0)) + _adj(eff, bid, z, nt)
        running = total if running is None else min(total, running)
        out[bid] = running
    return out


def compose(cf_log, eff_log):
    """Counterfactual + effect -> (game level, extra level), both in LEVELS.

    Multiplies levels rather than adding logs. `nowcast/predict.compose` explains
    why and `tests/test_citywide_invariants.py` pins it: adding in log space agrees
    to a fraction of a percent at large counts and diverges exactly where the
    counts are small, which is most of this panel.
    """
    cf = np.expm1(np.clip(np.asarray(cf_log, dtype=float), 0, None))
    mult = np.exp(np.asarray(eff_log, dtype=float))
    return cf * mult, cf * (mult - 1.0)
