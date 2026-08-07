"""The effect layer, re-estimated at the website's ring edges.

The GBM is a counterfactual: it trains on event-free hours with no event features
and answers "what would this block look like at 8pm with no game?". It cannot tell
you the lift. Everything in this module is the other half:

    predicted game evening = counterfactual x (1 + effect)

`nowcast/effects.py` already estimates that effect, but at ITS bands
(0-500 / 500m-1km / 1-2km / 2-4km / >4km) and as five flat steps. The site needs
three things that estimator does not provide, so this module adds them without
touching it:

  1. THE CANONICAL RING EDGES. The site, and the team's own build_silver
     RING_EDGES_M standard, cut at 0-250 / 250-500 / 500m-1km / 1-2.5km / 2.5-5km.
     Serving a number cut at different edges under those labels would mislabel
     every bar on the page, and no test could catch it because both sets return
     five numbers.

  2. A SMOOTH DECAY. Five flat steps render as five flat rings on the map. Fitting
     the per-cell residuals with A*exp(-d/L) + c gives every cell its own
     multiplier and keeps the gradient, which is also closer to what the physical
     process looks like.

  3. AN ATTENDANCE AND DAY/NIGHT RESPONSE, so two different games do not return an
     identical lift. This is the piece that makes the "generalizable impact
     function" claim real rather than aspirational.

The band DiD keeps `effects._did`'s residual-difference form but compares game
days to control days WITHIN (month x weekend) strata, weighted by where the game
days sit (see did_by). This is a deliberate divergence from nowcast/effects.py,
which pools all strict-control days: game days are almost all Apr-Sep and
weekend-heavy, and whatever calendar structure the counterfactual fails to
absorb walks straight into an unmatched difference. Measured on control days
alone (a placebo, no game day involved), reweighting the pool to the game
calendar moved the STGNN's 1-2.5km band by -1.4 log points and its 2.5-5km band
by +2.7, which is what rendered the demo map's hollow ring: an inner band
suppressed to zero inside a positive outer one (PR #20 discussion,
2026-08-06). Under the matched comparison both arms agree band for band and
decay monotonically. Matching also makes the handler's long-standing label
"vs a matched non-game evening" true. The smooth decay is then CALIBRATED so
its activity-weighted aggregate reproduces that DiD exactly (see band_shifts).
So the smooth curve is a within-band interpolation of the measured result
rather than a competing estimate of it.

FOUR HONESTY RULES:

  - Estimate on 2023-2024 only. 2025 reads far hotter near the ballpark because
    the device panel thins, not because the economy changed.
  - Compare within calendar strata (month x weekend), never against the pooled
    year: the pooled version manufactures effects out of seasonal misfit.
  - If a coefficient's bootstrap CI spans zero, SHIP ZERO. `game_dollars.py`
    already does this beyond 4km and the discipline should not be selective.
  - The 0-250m ring contains exactly ONE cell (214m, 27 POIs). It is reported,
    but `HERO_BAND` points the headline at the 0-500m aggregate instead, because a
    hero number resting on a single 250-metre square is not a headline.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import polars as pl

from ..settings import settings
from .featurespec import EVENING, FEATURES, build_X, predict_cf

# The project's canonical metric rings (RING_EDGES_M in pipeline/build_silver.py),
# plus a sixth band. 136 of the 452 cells sit past 5km, out to 10.9km; without
# somewhere to put them 30% of the map would be unbanded and the site would
# silently render them as zero-lift, which is a different claim from "not modeled".
CANONICAL_BANDS = [
    ("b1", "0-250m", 0.0, 250.0),
    ("b2", "250-500m", 250.0, 500.0),
    ("b3", "500m-1km", 500.0, 1000.0),
    ("b4", "1-2.5km", 1000.0, 2500.0),
    ("b5", "2.5-5km", 2500.0, 5000.0),
    ("b6", "beyond 5km", 5000.0, float("inf")),
]
BAND_IDS = [b[0] for b in CANONICAL_BANDS]

# The headline tile. b1 is a single cell; b1+b2 is five cells and 169 POIs, which
# is the same pool nowcast/effects.py's published 0-500m number rests on.
HERO_BAND = ("b1", "b2")

# 2025 is excluded on purpose: see the module docstring.
EFFECT_WINDOW = ("2023-01-01", "2024-12-31")

# Matching strata for the DiD. Month x weekend is deliberately coarse: it
# leaves a healthy control count in every game stratum, and the 2026-08-06
# diagnostics showed finer matching (month x day-of-week) moves no band by
# more than 0.4 log points while thinning some strata to 1-2 control days.
STRATA = ["month", "weekend"]

N_BOOT = 1000
SEED = 0


def band_of(dist_m: float) -> str:
    for bid, _label, lo, hi in CANONICAL_BANDS:
        if lo <= dist_m < hi:
            return bid
    return CANONICAL_BANDS[-1][0]


def canonical_band_expr(col: str = "dist_venue_m") -> pl.Expr:
    """First-match-wins on the upper bound, mirroring effects._band_expr."""
    e = pl.when(pl.col(col) < CANONICAL_BANDS[0][3]).then(pl.lit(CANONICAL_BANDS[0][0]))
    for bid, _label, _lo, hi in CANONICAL_BANDS[1:]:
        e = e.when(pl.col(col) < hi).then(pl.lit(bid))
    return e.otherwise(pl.lit(CANONICAL_BANDS[-1][0])).alias("band")


# ------------------------------------------------------------------ residuals


def evening_residuals(booster, cats, df: pl.DataFrame,
                      hours: tuple = EVENING) -> pl.DataFrame:
    """Row-level residuals on evening hours only.

    Filtering to the evening BEFORE predicting rather than after is a 3x saving
    and changes nothing: residuals are row-wise, and every cell appears at every
    hour so the unit_code category set is unaffected. We still pass the category
    list explicitly through build_X rather than letting pandas re-derive it.
    """
    ev = df.filter(pl.col("hour").is_between(*hours))
    X = build_X({f: ev[f].to_numpy() for f in FEATURES}, cats)
    pred = predict_cf(booster, X)
    return ev.select(
        "date", "unit_id", "dist_venue_m", "giants_home", "is_control", "y"
    ).with_columns(
        pl.Series("pred", pred),
        (pl.col("y") - pl.Series("pred", pred)).alias("resid"),
    ).with_columns(canonical_band_expr())


def _window(res: pl.DataFrame) -> pl.DataFrame:
    lo, hi = EFFECT_WINDOW
    return res.filter(
        pl.col("date").is_between(pl.lit(lo).str.to_date(), pl.lit(hi).str.to_date())
    )


def day_frames(res: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, list, list]:
    """Collapse to day x band and day x cell, and list the game and control days.

    Day grain is the clustering unit: cell-hours within a day are heavily
    correlated and naive cell-hour t-stats overstate significance by roughly 3x.
    """
    w = _window(res)
    dayband = w.group_by(["date", "band"]).agg(
        pl.col("resid").mean().alias("resid"),
        pl.col("giants_home").first().alias("giants_home"),
        pl.col("is_control").first().alias("is_control"),
    )
    daycell = w.group_by(["date", "unit_id"]).agg(
        pl.col("resid").mean().alias("resid"),
        pl.col("dist_venue_m").first().alias("dist_venue_m"),
        pl.col("giants_home").first().alias("giants_home"),
        pl.col("is_control").first().alias("is_control"),
    )
    gd = dayband.filter(pl.col("giants_home"))["date"].unique().to_list()
    cd = dayband.filter(
        pl.col("is_control") & ~pl.col("giants_home")
    )["date"].unique().to_list()
    if not gd or not cd:
        raise ValueError(f"effect window has {len(gd)} game and {len(cd)} control days")
    return dayband, daycell, gd, cd


def _with_strata(frame: pl.DataFrame) -> pl.DataFrame:
    """Matching strata derived from `date` alone (polars weekday: 1=Mon..7=Sun),
    so residual frames need not carry calendar columns."""
    return frame.with_columns(
        pl.col("date").dt.month().alias("month"),
        (pl.col("date").dt.weekday() >= 6).alias("weekend"),
    )


def _mean_by(frame: pl.DataFrame, key: str, days, name: str) -> pl.DataFrame:
    """Mean residual per (stratum, key) over `days`, counting a day once per
    appearance.

    Joins rather than filters. A bootstrap draw contains duplicates and `is_in`
    would collapse them, turning every replicate into a ~63% subsample whose
    variance is about 24% too small. This is the same fix nowcast/effects.py's
    `_day_mean` carries; the reasoning is written out there.
    """
    sel = pl.DataFrame({"date": list(days)}, schema={"date": frame.schema["date"]})
    return _with_strata(sel.join(frame, on="date", how="inner")).group_by(
        [*STRATA, key]).agg(pl.col("resid").mean().alias(name))


def did_by(frame: pl.DataFrame, key: str, gd, cd) -> dict:
    """Calendar-matched DiD: game minus control WITHIN each (month, weekend)
    stratum, combined with weights proportional to the game days in the list
    passed, so bootstrap draws reweight by exactly what they drew.

    An unmatched difference is unbiased only if the counterfactual absorbs all
    calendar structure; measured placebo bias on control days alone reached
    +15 log points at the core (module docstring). A stratum missing from one
    side contributes nothing: for the real frames every stratum with a game
    day has control days (estimate_all prints the coverage), and a synthetic
    frame without them simply narrows the estimate to the covered strata.
    """
    g = _mean_by(frame, key, gd, "g")
    c = _mean_by(frame, key, cd, "c")
    sel = pl.DataFrame({"date": list(gd)}, schema={"date": frame.schema["date"]})
    w = _with_strata(sel).group_by(STRATA).agg(pl.len().alias("n_g"))
    j = g.join(c, on=[*STRATA, key], how="inner").join(w, on=STRATA, how="inner")
    agg = j.group_by(key).agg(
        ((pl.col("g") - pl.col("c")) * pl.col("n_g")).sum().alias("num"),
        pl.col("n_g").sum().alias("den"),
    )
    return {r[key]: r["num"] / r["den"] for r in agg.iter_rows(named=True)}


# ------------------------------------------------------------------ decay


def _decay(d, A, L, c):
    return A * np.exp(-d / L) + c


def fit_decay(cell_did: dict, dist: dict, weight: dict) -> dict:
    """Weighted NLS of A*exp(-d/L) + c on the per-cell DiD, in log-effect space.

    Weighted by each cell's mean evening activity, so the curve is pulled by the
    cells that carry the traffic rather than by the long thin tail.

    The weighted R2 comes out around 0.19, which looks alarming until you note
    what is being fitted: a per-cell, day-grain difference-in-differences, which is
    among the noisiest quantities this panel produces. The curve is not carrying
    the estimate. The BAND aggregates carry it, and band_shifts pins those to the
    published DiD exactly; the curve only decides how a band's total is
    distributed across the cells inside it.

    Do not expect L to match the 750m-1km spatial correlation length in
    docs/PIPELINE.md. That number is how two cells' residuals covary with the
    distance BETWEEN THEM; this is how the game effect falls off with distance FROM
    THE VENUE. Different quantities, no reason to agree.
    """
    from scipy.optimize import curve_fit

    units = sorted(cell_did)
    d = np.array([dist[u] for u in units], dtype=float)
    y = np.array([cell_did[u] for u in units], dtype=float)
    w = np.array([weight.get(u, 0.0) for u in units], dtype=float)
    ok = np.isfinite(d) & np.isfinite(y) & (w > 0)
    d, y, w = d[ok], y[ok], w[ok]

    p0 = [max(y.max(), 0.1), 800.0, 0.0]
    bounds = ([0.0, 100.0, -0.5], [10.0, 8000.0, 0.5])
    popt, _ = curve_fit(_decay, d, y, p0=p0, bounds=bounds,
                        sigma=1.0 / np.sqrt(w), absolute_sigma=False, maxfev=20000)
    A, L, c = (float(v) for v in popt)
    resid = y - _decay(d, A, L, c)
    ss = float(1 - np.sum(w * resid ** 2) / np.sum(w * (y - np.average(y, weights=w)) ** 2))
    return {"A": A, "L": L, "c": c, "weighted_r2": ss, "n_cells": int(ok.sum())}


def band_shifts(decay: dict, band_did: dict, dist: dict, weight: dict,
                units_by_band: dict) -> dict:
    """Per-band offsets so the smooth curve reproduces the published band DiD.

    A smooth curve gives every cell its own multiplier, so a band's aggregate is an
    activity-weighted ratio of LEVELS and will not equal the band DiD unless we
    make it. Solving

        exp(s_b) = exp(did_b) * sum_c w_c / sum_c w_c * exp(smooth_c)

    keeps the within-band gradient while making the aggregate exact. Without this
    the site's band bars and the paper's numbers would quietly disagree.
    """
    out = {}
    for bid in BAND_IDS:
        us = [u for u in units_by_band.get(bid, []) if weight.get(u, 0) > 0]
        if not us or bid not in band_did:
            out[bid] = 0.0
            continue
        w = np.array([weight[u] for u in us])
        sm = np.array([_decay(dist[u], decay["A"], decay["L"], decay["c"]) for u in us])
        num = w.sum()
        den = float(np.sum(w * np.exp(sm)))
        out[bid] = float(band_did[bid] + np.log(num / den)) if den > 0 else 0.0
    return out


# ------------------------------------------------------------------ response


def attendance_response(dayband: pl.DataFrame, gd, cd, games: pl.DataFrame,
                        significant_bands=None,
                        n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """Per-band response of the game-day effect to attendance and day/night.

    Per-game effect is that game's day x band residual minus the control mean
    for the same band IN THE GAME'S OWN (month, weekend) STRATUM, which is
    exactly how the matched did_by decomposes. Without the stratum subtraction
    the per-game effects re-absorb the calendar composition the matched DiD
    removes, and the attendance slope picks up "summer weekends are busy"
    instead of "big crowds ripple further". Regressed on standardized
    attendance and a night indicator, bootstrapped over games.

    TWO GATES, both of which fired on the real data:

    1. Any coefficient whose own 95% interval spans zero is SHIPPED AS ZERO. With
       163 games and a noisy day-grain residual there is not much power here, and a
       coefficient we cannot sign should not move a number on a public page.

    2. `significant_bands` restricts the response to bands whose MAIN effect is
       itself distinguishable from zero. Without this the fit hands back a clean
       attendance slope of +0.030 (CI +0.014 to +0.047) for `beyond 5km`, a band
       whose own game-day effect is +0.4% with a CI straddling zero. A modifier on
       an undetectable effect is not a ripple; 5 to 11 km out it is picking up
       whatever else makes a big-crowd day busy citywide (weekend, weather,
       tourism). Shipping it would tell a business eight kilometres away that a
       sell-out is worth +3% to them, which the data does not support.

    A NOTE ON THE NIGHT COEFFICIENT. The core ring reads +0.21 in log space for
    night games, i.e. roughly +34% on a day game against +66% on a night game.
    Part of that is real and part is the measurement window: the effect is
    estimated on hours 16-23, which fully contains a 7pm first pitch but catches
    only the tail of a 1pm one. Read it as "the evening effect", not as "day games
    matter less".
    """
    rng = np.random.default_rng(seed)
    ctrl = _mean_by(dayband, "band", cd, "c")   # per (month, weekend, band)

    g = dayband.filter(pl.col("giants_home")).join(games, on="date", how="inner")
    g = g.filter(pl.col("attendance").is_not_null() & (pl.col("attendance") > 0))
    # Each game is scored against its own stratum's control mean. A game whose
    # stratum holds no control days has no calendar peers and is dropped rather
    # than scored against the pooled mean; zero rows drop on the real frames
    # (every game stratum has controls, see estimate_all's coverage print).
    g = _with_strata(g).join(ctrl, on=[*STRATA, "band"], how="left")
    n_unmatched = g.filter(pl.col("c").is_null()).height
    if n_unmatched:
        print(f"  attendance response: dropped {n_unmatched} game x band rows "
              f"with no control days in their stratum", flush=True)
    g = g.filter(pl.col("c").is_not_null())
    att = g["attendance"].to_numpy().astype(float)
    center, scale = float(att.mean()), float(att.std() or 1.0)

    out = {"center": center, "scale": scale, "beta": {}, "night": {},
           "alpha": {}, "n_games": {}, "beta_ci": {}, "night_ci": {}}

    for bid in BAND_IDS:
        sub = g.filter(pl.col("band") == bid)
        if significant_bands is not None and bid not in significant_bands:
            out["beta"][bid] = 0.0
            out["night"][bid] = 0.0
            out["alpha"][bid] = 0.0
            out["n_games"][bid] = int(sub.height)
            out["suppressed"] = out.get("suppressed", []) + [bid]
            continue
        if sub.height < 30:
            out["beta"][bid] = 0.0
            out["night"][bid] = 0.0
            out["alpha"][bid] = float((sub["resid"] - sub["c"]).mean()) if sub.height else 0.0
            out["n_games"][bid] = sub.height
            continue
        y = (sub["resid"] - sub["c"]).to_numpy()
        z = (sub["attendance"].to_numpy().astype(float) - center) / scale
        night = (sub["day_night"].to_numpy() == "night").astype(float)
        Xd = np.column_stack([np.ones_like(z), z, night])

        coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        boots = np.empty((n_boot, 3))
        n = len(y)
        for i in range(n_boot):
            idx = rng.integers(0, n, n)
            b, *_ = np.linalg.lstsq(Xd[idx], y[idx], rcond=None)
            boots[i] = b
        lo, hi = np.percentile(boots, [2.5, 97.5], axis=0)

        # ship zero unless the interval excludes it
        beta = float(coef[1]) if lo[1] * hi[1] > 0 else 0.0
        gam = float(coef[2]) if lo[2] * hi[2] > 0 else 0.0
        out["beta"][bid] = beta
        out["night"][bid] = gam
        out["alpha"][bid] = float(coef[0])
        out["n_games"][bid] = int(n)
        out["beta_ci"][bid] = [float(lo[1]), float(hi[1])]
        out["night_ci"][bid] = [float(lo[2]), float(hi[2])]
    return out


def bootstrap_band_ci(dayband: pl.DataFrame, gd, cd,
                      n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """Day-clustered bootstrap CI on the matched band DiD.

    Days are resampled WITHIN their (month, weekend) stratum so every draw
    keeps the game-calendar composition fixed. A pooled resample would let the
    composition term the matching removed wander back into the interval, and
    it would occasionally empty a stratum on one side, silently changing which
    strata the draw covers.
    """
    rng = np.random.default_rng(seed)

    def by_stratum(days) -> list[list]:
        sel = pl.DataFrame({"date": list(days)},
                           schema={"date": dayband.schema["date"]})
        return [g["date"].to_list()
                for _, g in _with_strata(sel).group_by(STRATA)]

    gstr, cstr = by_stratum(gd), by_stratum(cd)
    draws = {b: [] for b in BAND_IDS}
    for _ in range(n_boot):
        gdd = [s[i] for s in gstr for i in rng.integers(0, len(s), len(s))]
        cdd = [s[i] for s in cstr for i in rng.integers(0, len(s), len(s))]
        d = did_by(dayband, "band", gdd, cdd)
        for b in BAND_IDS:
            draws[b].append(d.get(b, np.nan))
    return {b: [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
            for b, v in draws.items()}


# ------------------------------------------------------------------ driver


def estimate_all(out_path: Path | None = None, n_boot: int = N_BOOT,
                 resid_fn=None) -> dict:
    """Estimate the whole effect layer and write effects.json.

    The estimator is model-agnostic: everything below the residual frame
    consumes only a `resid` column. `resid_fn(df) -> DataFrame` injects another
    model's residuals (same columns evening_residuals returns); None keeps the
    default GBM path byte-for-byte.
    """
    from ..nowcast.models import tier1_gbm as t

    df = t.load()
    if resid_fn is None:
        from .fitmodel import load_cached

        booster, cats = load_cached()
        print("  predicting evening residuals ...", flush=True)
        res = evening_residuals(booster, cats, df)
    else:
        print("  predicting evening residuals (injected source) ...", flush=True)
        res = resid_fn(df)
    dayband, daycell, gd, cd = day_frames(res)
    print(f"  effect window {EFFECT_WINDOW[0]}..{EFFECT_WINDOW[1]}: "
          f"{len(gd)} game days, {len(cd)} strict-control days", flush=True)

    # Matching coverage. The matched DiD silently narrows to the strata that
    # hold control days, so an empty stratum must be loud, not invisible.
    date_schema = {"date": dayband.schema["date"]}
    cov = (_with_strata(pl.DataFrame({"date": gd}, schema=date_schema))
           .group_by(STRATA).agg(pl.len().alias("n_g"))
           .join(_with_strata(pl.DataFrame({"date": cd}, schema=date_schema))
                 .group_by(STRATA).agg(pl.len().alias("n_c")),
                 on=STRATA, how="left")
           .with_columns(pl.col("n_c").fill_null(0)).sort(STRATA))
    uncontrolled = cov.filter(pl.col("n_c") == 0)
    n_uncontrolled_days = int(uncontrolled["n_g"].sum()) if uncontrolled.height else 0
    print(f"  matching: {cov.height} game strata (month x weekend); control "
          f"days per stratum min {cov['n_c'].min()}, median "
          f"{cov['n_c'].median():.0f}"
          + (f"; WARNING {uncontrolled.height} strata hold no controls, "
             f"dropping {n_uncontrolled_days} game days"
             if uncontrolled.height else ""),
          flush=True)

    # cell statics and activity weights (mean evening level on control days)
    w = _window(res).filter(pl.col("is_control") & ~pl.col("giants_home"))
    act = w.group_by("unit_id").agg(
        pl.col("y").exp().sub(1).mean().alias("level"),
        pl.col("dist_venue_m").first().alias("dist"),
    )
    weight = dict(zip(act["unit_id"], act["level"]))
    dist = dict(zip(act["unit_id"], act["dist"]))
    units_by_band: dict = {b: [] for b in BAND_IDS}
    for u, d in dist.items():
        units_by_band[band_of(d)].append(u)

    band_did = did_by(dayband, "band", gd, cd)
    cell_did = did_by(daycell, "unit_id", gd, cd)
    decay = fit_decay(cell_did, dist, weight)
    shifts = band_shifts(decay, band_did, dist, weight, units_by_band)
    print(f"  decay: A={decay['A']:.3f} L={decay['L']:.0f}m c={decay['c']:+.4f} "
          f"(weighted R2 {decay['weighted_r2']:.3f})", flush=True)

    print(f"  bootstrapping band CIs ({n_boot} draws) ...", flush=True)
    ci = bootstrap_band_ci(dayband, gd, cd, n_boot=n_boot)

    # A band whose own effect cannot be told from zero ships as zero, effect and
    # response alike. This is the discipline game_dollars.py already applies past
    # 4km, applied consistently instead of selectively. On the matched estimate
    # it fires for `beyond 5km` in both arms and, crucially, kills the spurious
    # far-field attendance slope that band would otherwise carry.
    significant = {b for b in BAND_IDS if b in ci and ci[b][0] * ci[b][1] > 0}
    dropped = [b for b in BAND_IDS if b not in significant]
    if dropped:
        print(f"  bands not distinguishable from zero, shipped as zero: "
              f"{', '.join(dropped)}", flush=True)

    games = df.filter(pl.col("giants_home")).group_by("date").agg(
        pl.col("day_night").first(),
    ).join(_mlb_attendance(), on="date", how="left")
    print("  fitting attendance / day-night response ...", flush=True)
    resp = attendance_response(dayband, gd, cd, games,
                               significant_bands=significant, n_boot=n_boot)

    payload = {
        "schema": "oracle-ripple-effects/1",
        "effect_window": list(EFFECT_WINDOW),
        "evening_hours": list(EVENING),
        "n_game_days": len(gd),
        "n_control_days": len(cd),
        # additive, for auditability; the handler reads bands/decay/response only
        "estimator": {
            "matching": "month x weekend strata, game-day weighted",
            "n_game_strata": int(cov.height),
            "min_control_days_in_game_stratum": int(cov["n_c"].min()),
            "game_days_in_uncontrolled_strata": n_uncontrolled_days,
        },
        "bands": [
            {"id": bid, "label": label, "inner_m": lo,
             "outer_m": None if hi == float("inf") else hi,
             # did_log/shift are what the handler applies; both are forced to 0
             # for a band we cannot distinguish from zero. did_log_raw keeps the
             # unsuppressed point estimate so the suppression stays auditable.
             "did_log": band_did.get(bid, 0.0) if bid in significant else 0.0,
             "shift": shifts.get(bid, 0.0) if bid in significant else 0.0,
             "significant": bid in significant,
             "did_log_raw": band_did.get(bid),
             "lift_pct": (100 * (np.exp(band_did[bid]) - 1)
                          if bid in significant and bid in band_did else 0.0),
             "lift_pct_raw": None if band_did.get(bid) is None
                             else 100 * (np.exp(band_did[bid]) - 1),
             "ci95_pct": None if bid not in ci else
                         [100 * (np.exp(ci[bid][0]) - 1), 100 * (np.exp(ci[bid][1]) - 1)],
             "n_cells": len(units_by_band.get(bid, []))}
            for bid, label, lo, hi in CANONICAL_BANDS
        ],
        "hero_band": list(HERO_BAND),
        "decay": decay,
        "response": resp,
    }
    dest = out_path or (settings.data_dir / "bronze_sf" / "effects.json")
    Path(dest).write_text(json.dumps(payload, indent=2, default=float) + "\n")
    print(f"  wrote {dest}", flush=True)
    return payload


def _mlb_attendance() -> pl.DataFrame:
    from ..io import duckdb_s3
    from .spine_2026 import MLB, _src

    return duckdb_s3().execute(
        f"""SELECT date::DATE AS date, TRY_CAST(attendance AS INTEGER) AS attendance
            FROM read_csv('{_src(MLB)}', all_varchar=true)
            WHERE TRY_CAST(attendance AS INTEGER) > 0"""
    ).pl()


if __name__ == "__main__":  # pragma: no cover
    p = estimate_all()
    print()
    r = p["response"]
    for b in p["bands"]:
        ci = b["ci95_pct"]
        ci_s = f"[{ci[0]:+6.1f}, {ci[1]:+6.1f}]" if ci else "  n/a"
        flag = "" if b["significant"] else f"  SUPPRESSED (raw {b['lift_pct_raw']:+.2f}%)"
        print(f"  {b['label']:>11s}  n={b['n_cells']:3d}  "
              f"lift {b['lift_pct']:+7.2f}%  CI95 {ci_s}  shift {b['shift']:+.4f}"
              f"  att {r['beta'].get(b['id'], 0):+.4f}  night {r['night'].get(b['id'], 0):+.4f}"
              f"{flag}")
