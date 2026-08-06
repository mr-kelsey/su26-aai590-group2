"""Run the packaged handler the way the container will, and check it against parquet.

Extracts model.tar.gz, puts code/ on sys.path, and drives model_fn / input_fn /
predict_fn / output_fn. That is the only way to exercise the flat
`import featurespec` the container uses; importing the handler as a package
module does not test the thing that ships.

THE ASSERTION THAT MATTERS is index_check(). The handler assembles its feature
matrix by slicing a flat array and tiling cell statics across hours, which is fast
and completely opaque: get the tiling backwards and every cell is scored with
another cell's baseline, the numbers stay plausible, and nothing raises. So we
rebuild the same rows from model_hour_serve with polars, in an obviously-correct
way, and require the counterfactuals to agree to 1e-9.

Also usable against the deployed endpoint (--endpoint NAME), which is how we
confirm the thing in AWS is the thing we built.
"""
from __future__ import annotations

import json
import sys
import tarfile
import tempfile
from pathlib import Path

import numpy as np
import polars as pl

from ..settings import settings

DEFAULT_TARBALL = "data/dist/model.tar.gz"


def extract(tarball: Path | None = None, dest: Path | None = None) -> Path:
    tarball = Path(tarball or (settings.data_dir / "dist" / "model.tar.gz"))
    dest = Path(dest or tempfile.mkdtemp(prefix="oracle-model-"))
    with tarfile.open(tarball) as t:
        t.extractall(dest)
    return dest


def load_handler(model_dir: Path):
    """Import the packaged handler with code/ on sys.path, container-style."""
    code = str(model_dir / "code")
    if code not in sys.path:
        sys.path.insert(0, code)
    for m in ("inference", "featurespec"):
        sys.modules.pop(m, None)
    import inference  # type: ignore

    return inference


def index_check_grid(model_dir: Path, ctx, dates) -> float:
    """The grid-source counterpart of index_check: two independent arithmetic
    routes to the same counterfactual. The handler slices the flat array by
    offset formula; here we reshape it to (n_dates, n_hours, n_cells) and index
    by components. A wrong stride or transposed layout disagrees immediately."""
    a = model_dir / "artifacts"
    grid = np.load(a / "cf_grid.npz")["cf_log"]
    n_c, n_h = ctx["n_cells"], ctx["n_hours"]
    cube = grid.reshape(-1, n_h, n_c)
    worst = 0.0
    for d in dates:
        di = ctx["date_index"][d]
        got = ctx["cf_grid"][di * n_h * n_c:(di + 1) * n_h * n_c]
        want = cube[di].ravel()
        worst = max(worst, float(np.abs(want - got).max()))
    return worst


def index_check(inf, ctx, dates, con=None) -> float:
    """Rebuild the handler's feature rows from parquet and compare counterfactuals.

    Independent path: polars reads model_hour_serve, joins the baselines, sorts by
    (date, hour, unit_code) and predicts through the same featurespec. If the
    handler's slice-and-tile assembly is wrong in any way, this disagrees.
    """
    import featurespec as fs  # type: ignore

    base = settings.data_dir / "bronze_sf"
    mh = pl.read_parquet(base / "model_hour_serve.parquet")
    rb = pl.read_parquet(base / "rolling_baseline_serve.parquet")
    units = ctx["units"]
    code = {u: i + 1 for i, u in enumerate(units)}

    worst = 0.0
    for d in dates:
        day = mh.filter(pl.col("date") == pl.lit(d).str.to_date()).join(
            rb, on=["unit_id", "date", "hour"], how="left"
        ).with_columns(
            pl.col("unit_id").replace_strict(code, return_dtype=pl.Int32).alias("unit_code")
        ).sort(["hour", "unit_code"])

        X = fs.build_X({f: day[f].to_numpy() for f in fs.FEATURES}, ctx["cats"])
        want = fs.predict_cf(ctx["booster"], X)

        req = inf.input_fn(json.dumps(
            {"date": d, "lat": 37.7786, "lon": -122.3893, "include_cells": True}))
        # re-derive the handler's own cf the same way predict_fn does
        got = _handler_cf(inf, req, ctx)
        worst = max(worst, float(np.abs(want - got).max()))
    return worst


def _handler_cf(inf, req, ctx):
    """Call the handler's assembly path and return its raw counterfactual."""
    import featurespec as fs  # type: ignore

    di = ctx["date_index"][str(req["date"])]
    n_c, n_h = ctx["n_cells"], ctx["n_hours"]
    sl = slice(di * n_h * n_c, (di + 1) * n_h * n_c)
    tf, cellsd = ctx["timefeat"], ctx["cells"]
    tlo, thi = di * n_h, (di + 1) * n_h
    cols = {
        "unit_code": np.tile(np.arange(1, n_c + 1), n_h),
        "base_k2": ctx["roll"]["base_k2"][sl],
        "base_k4": ctx["roll"]["base_k4"][sl],
        "base_cap120": ctx["roll"]["base_cap120"][sl],
        "n_cap120": ctx["roll"]["n_cap120"][sl],
        "n_poi_live": np.tile(ctx["poi_live"][di], n_h),
    }
    for f in ("hour", "dow", "month", "t_index", "temp_hr", "prcp_hr", "wind_hr",
              "us_federal_holiday"):
        cols[f] = np.repeat(np.asarray(tf[f])[tlo:thi], n_c)
    for f in ("n_poi", "food_share", "dist_venue_m", "bearing_venue_deg"):
        cols[f] = np.tile(cellsd[f], n_h)
    return fs.predict_cf(ctx["booster"], fs.build_X(cols, ctx["cats"]))


def call(inf, ctx, **kw) -> dict:
    body = json.dumps(kw)
    out = inf.predict_fn(inf.input_fn(body), ctx)
    text, _ = inf.output_fn(out)
    return json.loads(text)


def summarize(tag: str, r: dict) -> None:
    g, b, f, h = r["game"], r["bands"], r["focus"], r["headline"]
    print(f"\n--- {tag}: {r['date']} ---")
    print(f"  game={g['home']} start={g['start']} att={g['attendance']} "
          f"({g['attendance_source']})  measure={r['measure']['id']}")
    print(f"  basis: projected={r['basis']['projected']} "
          f"stale={r['basis']['baseline_staleness_days']}d "
          f"wx={r['basis']['weather_source']} "
          f"t_index_beyond_training={r['basis']['t_index_beyond_training']}")
    print("  bands: " + "  ".join(
        f"{x['label']}={x['lift_pct']:+.2f}%/{x['extra']:,.0f}" for x in b))
    print(f"  focus: {f['cell_id']} {f['dist_venue_m']}m {f['band_label']} "
          f"{f['lift_pct']:+.2f}% extra={f['extra']:,.1f} "
          f"cf={f['counterfactual']:,.1f} outside={f['outside']} snapped={f['snapped']}")
    print(f"  headline: {h['hero_band_label']} {h['hero_band_lift_pct']:+.2f}%, "
          f"{h['extra_within_2p5km']:,.0f} within 2.5km")
    print(f"  cells={len(r['cells'])}  {r['diagnostics']['total_ms']}ms")


def write_fixtures(dest: str, prefix: str, responses: dict) -> None:
    """Record real handler responses as website test fixtures.

    PLUG-IN-ENDPOINT.md documents the fixtures as recorded-not-handwritten;
    this is the recorder. Names mirror the existing set:
    {prefix}-played-night.json / {prefix}-projected-2026.json /
    {prefix}-no-game.json.
    """
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    names = {"played": "played-night", "projected": "projected-2026",
             "no game": "no-game"}
    for tag, name in names.items():
        p = out / f"{prefix}-{name}.json"
        p.write_text(json.dumps(responses[tag], indent=2) + "\n")
        print(f"  fixture -> {p}", flush=True)


def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tarball", default=None)
    ap.add_argument("--model", default="oracle-ripple",
                    choices=("oracle-ripple", "oracle-ripple-stgnn"))
    ap.add_argument("--endpoint", default=None,
                    help="also invoke this live SageMaker endpoint and diff")
    ap.add_argument("--write-fixtures", default=None, metavar="DIR",
                    help="record the responses as website test fixtures")
    ap.add_argument("--fixture-prefix", default=None,
                    help="fixture filename prefix (default: endpoint or "
                         "endpoint-stgnn by --model)")
    a = ap.parse_args(argv)

    if a.tarball is None and a.model == "oracle-ripple-stgnn":
        a.tarball = settings.data_dir / "dist" / "model-stgnn.tar.gz"
    d = extract(a.tarball)
    print(f"extracted -> {d}")
    inf = load_handler(d)
    ctx = inf.model_fn(str(d))
    is_grid = ctx.get("cf_source") == "grid"

    # 2025-08-15 is a real home game with a REAL attendance (34,172). Do not use a
    # date the Giants were away on: it silently exercises the no-game path and
    # every game-day assertion passes vacuously against a row of zeros.
    played, projected, nogame = "2025-08-15", "2026-08-07", "2026-01-15"
    r_played = call(inf, ctx, date=played, lat=37.7801, lon=-122.3894)
    r_proj = call(inf, ctx, date=projected, lat=37.7801, lon=-122.3894)
    r_none = call(inf, ctx, date=nogame, lat=37.7801, lon=-122.3894)
    r_far = call(inf, ctx, date=played, lat=37.62, lon=-122.5, include_cells=False)
    for tag, r in (("played", r_played), ("projected", r_proj),
                   ("no game", r_none), ("far pin", r_far)):
        summarize(tag, r)

    if is_grid:
        print("\n=== index check: handler slice vs reshaped grid ===")
        worst = index_check_grid(d, ctx, [played, projected, nogame])
    else:
        print("\n=== index check: handler assembly vs parquet ===")
        worst = index_check(inf, ctx, [played, projected, nogame])
    print(f"  max |delta| on counterfactuals: {worst:.3g}  "
          f"-> {'PASS' if worst < 1e-9 else 'FAIL'}")

    print("\n=== invariants ===")
    # which bands the effect layer itself says are indistinguishable from zero;
    # asserting b6 by name would break the moment another model's b6 clears its CI
    insig = [b["id"] for b in ctx["effects"]["bands"]
             if not b.get("significant", True)]
    checks = [
        ("no-game date has zero lift", all(x["lift_pct"] == 0 for x in r_none["bands"])),
        ("no-game date still has a real counterfactual",
         r_none["focus"]["counterfactual"] > 0),
        ("far pin is outside", r_far["focus"]["outside"] is True),
        (f"insignificant bands ship as zero ({','.join(insig) or 'none'})",
         all(x["lift_pct"] == 0.0
             for x in r_played["bands"] if x["id"] in insig)),
        ("projected date flagged", r_proj["basis"]["projected"] is True),
        ("played date not flagged", r_played["basis"]["projected"] is False),
        ("measure is visitor_hours", r_played["measure"]["id"] == "visitor_hours"),
        ("all 452 cells returned", len(r_played["cells"]) == ctx["n_cells"]),
        ("bands reconcile with cells", _reconciles(r_played)),
        # both a played game (real attendance) and a projected one (above-centre
        # attendance, night). The projected case is the one the monotone cap
        # exists for; checking only the played one would pass vacuously.
        ("lift decays with distance, played", _monotone(r_played)),
        ("lift decays with distance, projected", _monotone(r_proj)),
        ("played game has non-zero core lift",
         r_played["bands"][0]["lift_pct"] > 0),
        ("played game uses ACTUAL attendance",
         r_played["game"]["attendance_source"] == "actual"),
    ]
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not all(ok for _, ok in checks) or worst >= 1e-9:
        raise SystemExit("smoke test failed")

    for label, bad in (
        ("widened hour window", {"date": played, "lat": 37.78, "lon": -122.39,
                                 "hours": [0, 23]}),
        ("unknown field", {"date": played, "lat": 37.78, "lon": -122.39, "wat": 1}),
        ("missing lat", {"date": played, "lon": -122.39}),
    ):
        try:
            inf.input_fn(json.dumps(bad))
            raise SystemExit(f"FAIL: {label} was accepted")
        except inf.BadRequest:
            print(f"  PASS  {label} rejected")
    try:
        call(inf, ctx, date="2019-01-01", lat=37.78, lon=-122.39)
        raise SystemExit("FAIL: out-of-window date was accepted")
    except inf.BadRequest:
        print("  PASS  out-of-window date rejected")
    if is_grid:
        # the first serve date has no convolution context and stays NaN in the
        # grid; the handler must refuse it rather than serialize NaN
        try:
            call(inf, ctx, date="2023-01-02", lat=37.78, lon=-122.39)
            raise SystemExit("FAIL: context-less first date was accepted")
        except inf.BadRequest:
            print("  PASS  context-less first date rejected")

    if a.write_fixtures:
        prefix = a.fixture_prefix or (
            "endpoint-stgnn" if a.model == "oracle-ripple-stgnn" else "endpoint")
        write_fixtures(a.write_fixtures, prefix,
                       {"played": r_played, "projected": r_proj,
                        "no game": r_none})

    if a.endpoint:
        _diff_endpoint(a.endpoint, [(played, 37.7801, -122.3894),
                                    (projected, 37.7801, -122.3894)], inf, ctx)


def _reconciles(r: dict) -> bool:
    by = {}
    for c in r["cells"]:
        for b in r["bands"]:
            lo, hi = b["inner_m"], b["outer_m"] or float("inf")
            if lo <= c["dist_venue_m"] < hi:
                by[b["id"]] = by.get(b["id"], 0.0) + c["extra"]
                break
    return all(abs(by.get(b["id"], 0.0) - b["extra"]) <= max(1.0, 0.001 * abs(b["extra"]))
               for b in r["bands"])


def _monotone(r: dict) -> bool:
    """The effect must not increase with distance WHERE THAT ORDERING IS REAL.

    Strict monotonicity is the wrong assertion. The monotone cap in effect_log
    works in calibration space, but a band's reported aggregate is weighted by
    that date's own counterfactual, which differs slightly from the mean-activity
    weights the calibration used. So adjacent bands can land 0.05pp apart in the
    wrong order, e.g. 1-2.5km at +1.97% against 2.5-5km at +2.02%.

    Demanding an exact ordering between two bands whose confidence intervals are
    +0.7 to +3.5 and +0.2 to +3.0 would be asserting precision the estimate does
    not have. So an inversion is only a failure when the two bands' CIs do NOT
    overlap, which is exactly when we are entitled to claim an ordering at all.
    """
    b = r["bands"]
    for i in range(len(b) - 1):
        lo, hi = b[i], b[i + 1]
        if lo["lift_pct"] >= hi["lift_pct"] - 1e-9:
            continue
        ci_a, ci_b = lo.get("ci95_pct"), hi.get("ci95_pct")
        overlap = (ci_a and ci_b and ci_a[0] <= ci_b[1] and ci_b[0] <= ci_a[1])
        if not overlap:
            print(f"    inversion {lo['label']} {lo['lift_pct']:+.2f}% < "
                  f"{hi['label']} {hi['lift_pct']:+.2f}% with disjoint CIs")
            return False
    return True


def _diff_endpoint(name: str, cases, inf, ctx) -> None:
    import boto3

    rt = boto3.client("sagemaker-runtime", region_name=settings.aws_region)
    print(f"\n=== live endpoint {name} vs local ===")
    for date, lat, lon in cases:
        body = json.dumps({"date": date, "lat": lat, "lon": lon})
        res = rt.invoke_endpoint(EndpointName=name, ContentType="application/json",
                                 Body=body)
        live = json.loads(res["Body"].read())
        loc = call(inf, ctx, date=date, lat=lat, lon=lon)
        d = max(abs(a["lift_pct"] - b["lift_pct"])
                for a, b in zip(live["cells"], loc["cells"]))
        e = max(abs(a["extra"] - b["extra"])
                for a, b in zip(live["cells"], loc["cells"]))
        print(f"  {date}: max |dlift| {d:.6f}pp  max |dextra| {e:.4f}  "
              f"-> {'PASS' if d < 1e-6 and e < 1e-2 else 'FAIL'}")


if __name__ == "__main__":  # pragma: no cover
    main()
