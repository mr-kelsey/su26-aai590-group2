"""SageMaker inference handler for the Oracle Park game-evening ripple.

Runs inside the SageMaker scikit-learn container. Written to Python 3.8 syntax
and importing only numpy, pandas, lightgbm and the packaged featurespec, because
the container's Python version is not something worth discovering the hard way.

WHAT IT COMPUTES

The GBM is a counterfactual: it answers "what would this block look like at 8pm
with no game?". So a request is two steps, not one:

    counterfactual = GBM(features for this date, hour, cell)
    game evening   = counterfactual x (1 + effect(distance, attendance, day/night))

with the effect coming entirely from effects.json. Both steps use functions in
featurespec.py, which is the same file the training side imports, byte for byte.

A non-game date returns the counterfactual with zero lift rather than an error or
a hardcoded zero, which is what lets the site say "expect a normal evening, about
N visitor-hours" instead of just "no game".

UNITS ARE VISITOR-HOURS, not visits. One unit is one estimated visitor present
during one hourly bucket, so a four-hour visit counts four times. The window is
hours 16-23 and cannot be widened: the effects were estimated on that window and
applying them to a whole day inflates the answer by roughly 3x.

FAILING AT STARTUP IS THE FEATURE. model_fn re-assembles the golden rows through
the packaged featurespec and compares them to the recorded predictions. If they
disagree it raises, the container fails its health check, the endpoint goes to
Failed, and traffic never moves to it. A wrong endpoint that will not start beats
a wrong endpoint that answers.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import numpy as np

import featurespec as fs

SCHEMA = "oracle-ripple/1"
SNAP_M = 400.0          # matches the site's simulate.ts; keep them equal
GRID_DLAT = 0.00225
GRID_DLON = 0.00284
MEASURE = {"id": "visitor_hours", "noun": "visitor-hours"}


# ---------------------------------------------------------------- load


def model_fn(model_dir):
    import lightgbm as lgb

    t0 = time.time()
    a = os.path.join(model_dir, "artifacts")
    if not os.path.isdir(a):
        a = model_dir

    def _json(name):
        with open(os.path.join(a, name)) as f:
            return json.load(f)

    man = _json("manifest.json")
    booster = lgb.Booster(model_file=os.path.join(model_dir, "model.txt"))
    cats = booster.pandas_categorical[0]

    units = _json("unit_ids.json")
    if list(cats) != list(range(1, len(units) + 1)):
        raise RuntimeError(
            "booster categories are not 1..%d; the unit_code map is not what the "
            "serve path assumes" % len(units)
        )

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "featurespec.py"), "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if got != man["featurespec_sha256"]:
        raise RuntimeError(
            "featurespec.py in the tarball (%s) is not the one the artifacts were "
            "built with (%s)" % (got[:12], man["featurespec_sha256"][:12])
        )

    cells = np.load(os.path.join(a, "cells.npz"))
    timefeat = np.load(os.path.join(a, "timefeat.npz"))
    poi = np.load(os.path.join(a, "poi_live.npz"))
    roll = np.load(os.path.join(a, "roll.npz"))
    dates = _json("dates.json")
    eff = _json("effects.json")

    ctx = {
        "manifest": man,
        "booster": booster,
        "cats": cats,
        "units": units,
        "unit_index": {u: i for i, u in enumerate(units)},
        "cells": {k: cells[k] for k in cells.files},
        "timefeat": {k: timefeat[k] for k in timefeat.files},
        "poi_live": poi["n_poi_live"],
        "poi_held": poi["held"],
        "roll": {k: roll[k] for k in roll.files},
        "dates": dates,
        "date_index": {d["date"]: i for i, d in enumerate(dates)},
        "effects": eff,
        "n_cells": man["n_cells"],
        "n_hours": man["evening_hours"][1] - man["evening_hours"][0] + 1,
        "hour0": man["evening_hours"][0],
        "band_of": [fs.band_of(float(d), eff["bands"]) for d in cells["dist_venue_m"]],
    }
    _check_golden(a, ctx)
    print("model_fn ready in %.1fs (%d cells, %d dates)"
          % (time.time() - t0, ctx["n_cells"], len(dates)), flush=True)
    return ctx


def _check_golden(a, ctx):
    g = np.load(os.path.join(a, "golden.npz"))
    cats = np.asarray(ctx["cats"])
    cols = {}
    for f in fs.FEATURES:
        cols[f] = cats[g[f]] if f in fs.CATEGORICAL else g[f]
    X = fs.build_X(cols, ctx["cats"])
    yhat = fs.predict_cf(ctx["booster"], X)
    np.testing.assert_allclose(yhat, g["yhat"], atol=1e-9)


# ---------------------------------------------------------------- input


class BadRequest(ValueError):
    pass


def input_fn(request_body, content_type="application/json"):
    if content_type and "json" not in content_type:
        raise BadRequest("content_type must be application/json, got %r" % content_type)
    try:
        req = json.loads(request_body)
    except Exception as e:
        raise BadRequest("body is not valid JSON: %s" % e)
    if not isinstance(req, dict):
        raise BadRequest("body must be a JSON object")

    known = {"schema", "date", "lat", "lon", "hours", "include_cells",
             "attendance", "day_night", "bands_m", "focus_cell_id"}
    unknown = set(req) - known
    if unknown:
        raise BadRequest("unknown fields: %s" % ", ".join(sorted(unknown)))
    if not req.get("date"):
        raise BadRequest("date is required")
    for k in ("lat", "lon"):
        if req.get(k) is None:
            raise BadRequest("%s is required" % k)
        try:
            req[k] = float(req[k])
        except Exception:
            raise BadRequest("%s must be a number" % k)

    # Rejected here rather than silently clamped. The effect layer is estimated on
    # hours 16-23; applying it to a whole day inflates the answer by roughly 3x,
    # and a caller who asked for a wider window should be told no, not handed a
    # narrower answer they did not ask for.
    if req.get("hours") is not None and list(req["hours"]) != list(fs.EVENING):
        raise BadRequest(
            "hours must be %s: the effect layer is estimated on that window and "
            "applying it to a wider one inflates the result by about 3x"
            % (list(fs.EVENING),)
        )
    return req


# ---------------------------------------------------------------- predict


def _resolve_focus(ctx, lat, lon):
    """The cell holding the pin, else the nearest centroid within SNAP_M.

    Same rule and same 400m radius as the site's simulate.ts. If the two ever
    disagree the site would draw its focus outline on a different block from the
    one the number describes.
    """
    gi = int(np.floor(lat / GRID_DLAT))
    gj = int(np.floor(lon / GRID_DLON))
    exact = ctx["unit_index"].get("c%d_%d" % (gi, gj))
    if exact is not None:
        return exact, False, False

    la, lo = ctx["cells"]["lat"], ctx["cells"]["lon"]
    dy = (lat - la) * 111320.0
    dx = (lon - lo) * 111320.0 * np.cos(np.radians(la))
    d = np.sqrt(dx * dx + dy * dy)
    i = int(np.argmin(d))
    if d[i] <= SNAP_M:
        return i, True, False
    return None, False, True


def predict_fn(req, ctx):
    t0 = time.time()
    date = str(req["date"])
    di = ctx["date_index"].get(date)
    if di is None:
        raise BadRequest(
            "date %s outside the served window %s..%s"
            % (date, ctx["manifest"]["serve_window"][0], ctx["manifest"]["serve_window"][1])
        )
    meta = ctx["dates"][di]

    lo_h, hi_h = ctx["manifest"]["evening_hours"]
    hours = req.get("hours") or [lo_h, hi_h]
    if list(hours) != [lo_h, hi_h]:
        raise BadRequest(
            "hours must be [%d, %d]: the effect layer is estimated on that window "
            "and applying it to a wider one inflates the result by about 3x"
            % (lo_h, hi_h)
        )

    n_c, n_h = ctx["n_cells"], ctx["n_hours"]
    lo = di * n_h * n_c
    hi = lo + n_h * n_c
    sl = slice(lo, hi)

    # cell statics tile across hours; time features repeat across cells
    cellsd = ctx["cells"]
    tf = ctx["timefeat"]
    tlo = di * n_h
    thi = tlo + n_h
    tile = lambda v: np.tile(np.asarray(v), n_h)                       # noqa: E731
    rep = lambda v: np.repeat(np.asarray(v)[tlo:thi], n_c)             # noqa: E731

    cols = {
        "unit_code": tile(np.arange(1, n_c + 1)),
        "base_k2": ctx["roll"]["base_k2"][sl],
        "base_k4": ctx["roll"]["base_k4"][sl],
        "base_cap120": ctx["roll"]["base_cap120"][sl],
        "n_cap120": ctx["roll"]["n_cap120"][sl],
        "n_poi_live": tile(ctx["poi_live"][di]),
    }
    for f in ("hour", "dow", "month", "t_index", "temp_hr", "prcp_hr", "wind_hr",
              "us_federal_holiday"):
        cols[f] = rep(tf[f])
    for f in ("n_poi", "food_share", "dist_venue_m", "bearing_venue_deg"):
        cols[f] = tile(cellsd[f])

    X = fs.build_X(cols, ctx["cats"])
    cf_log = fs.predict_cf(ctx["booster"], X)

    dist = tile(cellsd["dist_venue_m"])
    bands = tile(np.asarray(ctx["band_of"], dtype=object))
    is_game = bool(meta["giants_home"])
    attendance = req.get("attendance", meta["attendance"])
    day_night = req.get("day_night", meta["day_night"])
    if is_game and attendance is None:
        attendance = ctx["effects"]["response"]["center"]

    if is_game:
        eff_log = fs.effect_log(
            dist, bands,
            np.full(len(dist), float(attendance)),
            np.full(len(dist), 1.0 if day_night == "night" else 0.0),
            ctx["effects"],
        )
    else:
        eff_log = np.zeros(len(dist))

    game_lvl, extra_lvl = fs.compose(cf_log, eff_log)

    # sum the evening hours per cell
    shape = (n_h, n_c)
    cf_c = np.expm1(np.clip(cf_log, 0, None)).reshape(shape).sum(axis=0)
    game_c = game_lvl.reshape(shape).sum(axis=0)
    extra_c = extra_lvl.reshape(shape).sum(axis=0)
    lift_c = np.where(cf_c > 0, game_c / np.maximum(cf_c, 1e-9) - 1.0, 0.0)

    units = ctx["units"]
    cell_band = ctx["band_of"]
    out_cells = [
        {"id": units[i],
         "lift_pct": round(float(lift_c[i]) * 100, 3),
         "extra": round(float(extra_c[i]), 2),
         "counterfactual": round(float(cf_c[i]), 2),
         "dist_venue_m": round(float(cellsd["dist_venue_m"][i]), 1)}
        for i in range(n_c)
    ] if req.get("include_cells", True) else []

    out_bands = []
    for b in ctx["effects"]["bands"]:
        m = np.array([cb == b["id"] for cb in cell_band])
        cfb, gb, eb = cf_c[m].sum(), game_c[m].sum(), extra_c[m].sum()
        out_bands.append({
            "id": b["id"], "label": b["label"],
            "inner_m": b["inner_m"], "outer_m": b["outer_m"],
            "lift_pct": round(float(gb / cfb - 1.0) * 100, 3) if cfb > 0 else 0.0,
            "extra": round(float(eb), 2),
            "counterfactual": round(float(cfb), 2),
            "n_cells": int(m.sum()),
            "ci95_pct": b.get("ci95_pct"),
            "significant": bool(b.get("significant", True)),
        })

    fi, snapped, outside = _resolve_focus(ctx, req["lat"], req["lon"])
    focus = {
        "cell_id": None if fi is None else units[fi],
        "dist_venue_m": None if fi is None else round(float(cellsd["dist_venue_m"][fi]), 1),
        "band_label": None if fi is None else next(
            b["label"] for b in out_bands if b["id"] == cell_band[fi]),
        "lift_pct": 0.0 if fi is None else round(float(lift_c[fi]) * 100, 3),
        "extra": 0.0 if fi is None else round(float(extra_c[fi]), 2),
        "counterfactual": 0.0 if fi is None else round(float(cf_c[fi]), 2),
        "outside": bool(outside),
        "snapped": bool(snapped),
    }

    within = sum(b["extra"] for b in out_bands if b["outer_m"] and b["outer_m"] <= 2500)
    hero = [b for b in out_bands if b["id"] in ctx["effects"]["hero_band"]]
    hero_cf = sum(b["counterfactual"] for b in hero) or 1.0
    hero_extra = sum(b["extra"] for b in hero)

    man = ctx["manifest"]
    return {
        "schema_version": SCHEMA,
        "model_version": man.get("model_version", "gbm-" + man["schema"].split("/")[-1]),
        "measure": MEASURE,
        "date": date,
        "window": {"hours": [lo_h, hi_h], "label": "4pm to 11pm"},
        "bands_m": [b["inner_m"] for b in out_bands] + [5000.0],
        "game": {
            "home": is_game,
            "n_games": meta["n_games"],
            "start": meta["day_night"],
            "first_pitch_hour": meta["first_pitch_hour"],
            "attendance": None if not is_game else float(attendance),
            "attendance_source": meta["attendance_source"] if is_game else None,
        },
        "basis": {
            "training_window": man["training_window"],
            "observed_panel_through": man["observed_panel_through"],
            "projected": bool(meta["projected"]),
            "baseline_staleness_days": meta["baseline_staleness_days"],
            "weather_source": meta["weather_source"],
            "t_index_beyond_training": bool(meta["t_index_beyond_training"]),
            "effect_window": ctx["effects"]["effect_window"],
        },
        "competing_events": {
            k: bool(meta[k]) for k in
            ("chase_day", "moscone_day", "citywide_day", "street_fair_day")
        },
        "bands": out_bands,
        "cells": out_cells,
        "focus": focus,
        "headline": {
            "extra_within_2p5km": round(float(within), 2),
            "hero_band_lift_pct": round(float(hero_extra / hero_cf) * 100, 3),
            "hero_band_label": " + ".join(b["label"] for b in hero),
            "window_label": ("game evening vs a matched non-game evening"
                             if is_game else "no Giants home game on this date"),
        },
        "diagnostics": {
            "n_rows_scored": int(len(cf_log)),
            "total_ms": int((time.time() - t0) * 1000),
        },
    }


def output_fn(prediction, accept="application/json"):
    # allow_nan=False on purpose: NaN is not valid JSON and JSON.parse on the
    # site would throw on it. Better to fail here, loudly, than to ship a body
    # the browser cannot read.
    return json.dumps(prediction, allow_nan=False), "application/json"
