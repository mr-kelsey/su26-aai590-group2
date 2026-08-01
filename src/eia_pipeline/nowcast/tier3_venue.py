"""Tier 3: held-out-venue generalisation via a crossover design.

With only one venue we cannot separate an event effect from the possibility that a particular neighbourhood simply behaves differently on summer evenings. Any model would happily attribute a Mission Bay quirk to the ballpark.

Chase Center gives us the test. It sits 1,188 m from Oracle Park and has 211 event days that do not coincide with a Giants game. We drop the 45 days that do coincide rather than controlling for them, since attributing a shared day to either venue would beg the question. Their 0-500 m bands share no cells at all, so we can estimate a 2x2:

                     bands centred on Oracle | bands centred on Chase
    Giants-only days        own-venue        |      cross
    Chase-only days           cross          |    own-venue

If the effect follows the venue, the diagonal is strong and the off-diagonal is weak. If instead we are picking up a generic Mission Bay evening pattern, all four cells light up and the near-field result means much less than it appears.

This is the strongest generalisation claim we can make from the data, and unlike a held-out time split it cannot be satisfied by a model that has simply memorised where the ballpark is.

One caveat on precision: Chase's inner ring holds only one qualifying cell against Oracle's five, because Mission Bay south of the ballpark is newer and lower-POI development. Chase's 0-500 m estimate is therefore noisier, and its interval deserves more weight than its point estimate.
"""
from __future__ import annotations

import numpy as np
import polars as pl

VENUES = {
    "oracle": (37.7786, -122.3893),   # Oracle Park, home plate
    "chase": (37.7680, -122.3877),    # Chase Center
}

BANDS = [("0-500m", 0, 500), ("500m-1km", 500, 1000), ("1-2km", 1000, 2000),
         ("2-4km", 2000, 4000), (">4km", 4000, 10 ** 9)]
EVENING = (16, 23)

TREATMENTS = {
    # disjoint by construction; shared days excluded from both
    "giants": (pl.col("giants_home") & ~pl.col("chase_day")),
    "chase": (pl.col("chase_day") & ~pl.col("giants_home")),
}


def _dist_expr(venue: str) -> pl.Expr:
    la, lo = VENUES[venue]
    return (((pl.col("lat") - la) * 111320.0) ** 2
            + ((pl.col("lon") - lo) * 111320.0 * np.cos(np.radians(la))) ** 2).sqrt()


def _band_expr(venue: str) -> pl.Expr:
    d = _dist_expr(venue)
    e = pl.when(d <= BANDS[0][2]).then(pl.lit(BANDS[0][0]))
    for name, lo, hi in BANDS[1:]:
        e = e.when(d <= hi).then(pl.lit(name))
    return e.otherwise(pl.lit(BANDS[-1][0])).alias("band")


def day_band(model, df: pl.DataFrame, venue: str, hours=EVENING) -> pl.DataFrame:
    """Residuals collapsed to day x band, banded around `venue`."""
    from .models.tier1_gbm import _xy

    X, _ = _xy(df)
    d = df.with_columns(pl.Series("pred", model.predict(X)))
    d = d.with_columns((pl.col("y") - pl.col("pred")).alias("resid"))
    ev = d.filter(pl.col("hour").is_between(*hours)).with_columns(_band_expr(venue))
    return ev.group_by(["date", "band"]).agg(
        pl.col("resid").mean().alias("resid"),
        pl.col("giants_home").first().alias("giants_home"),
        pl.col("chase_day").first().alias("chase_day"),
        pl.col("is_control").first().alias("is_control"),
    )


def _did(db: pl.DataFrame, tdays, cdays) -> dict:
    g = db.filter(pl.col("date").is_in(tdays)).group_by("band").agg(pl.col("resid").mean().alias("g"))
    k = db.filter(pl.col("date").is_in(cdays)).group_by("band").agg(pl.col("resid").mean().alias("k"))
    j = g.join(k, on="band")
    return {r["band"]: r["g"] - r["k"] for r in j.iter_rows(named=True)}


def estimate(db: pl.DataFrame, treatment: str, n_boot: int = 1500, seed: int = 0) -> pl.DataFrame:
    """Day-clustered bootstrap of the DiD for one treatment, in this frame's bands."""
    rng = np.random.default_rng(seed)
    td = db.filter(TREATMENTS[treatment])["date"].unique().to_numpy()
    cd = db.filter(pl.col("is_control")
                   & ~pl.col("giants_home") & ~pl.col("chase_day"))["date"].unique().to_numpy()
    point = _did(db, td.tolist(), cd.tolist())
    draws = {b: [] for b in point}
    for _ in range(n_boot):
        d = _did(db, rng.choice(td, len(td), True).tolist(),
                 rng.choice(cd, len(cd), True).tolist())
        for b, v in d.items():
            draws[b].append(v)
    rows = []
    for name, _, _ in BANDS:
        if name not in point:
            continue
        a = np.array(draws[name])
        rows.append({"band": name, "treatment": treatment, "n_treat": int(len(td)),
                     "effect_pct": (np.exp(point[name]) - 1) * 100,
                     "lo_pct": (np.exp(np.percentile(a, 2.5)) - 1) * 100,
                     "hi_pct": (np.exp(np.percentile(a, 97.5)) - 1) * 100,
                     "p": float(2 * min((a <= 0).mean(), (a >= 0).mean()))})
    return pl.DataFrame(rows)


def crossover(model, df: pl.DataFrame, n_boot: int = 1500) -> dict:
    """The full 2x2: each treatment estimated in each venue's distance frame."""
    out = {}
    for venue in VENUES:
        db = day_band(model, df, venue)
        for treatment in TREATMENTS:
            out[(treatment, venue)] = estimate(db, treatment, n_boot=n_boot)
    return out
