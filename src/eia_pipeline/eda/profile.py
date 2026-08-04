"""Profile the modelling panel: distributions, missingness, split drift, correlations.

This is the layer underneath `docs/04_data_exploration.md`. That file is a findings log
and every entry in it answers a modelling question. Nothing in it describes the panel
itself: what our target's distribution looks like, which columns are missing and whether
that missingness is structural or a gap, whether our three splits are drawn from
comparable populations. Those are the things a reviewer asks about first, and the things
that quietly invalidate a result when nobody checked them.

Two consumers and one set of numbers. `write_report()` lands `docs/07_data_profile.md` and
`eia_pipeline.eda.charts` figures 09-12 draw the same frames, so a number cannot appear in
our prose and our figures with two different values.

    uv run python -m eia_pipeline.eda.profile          # writes docs/07_data_profile.md

Everything reads `data/bronze_sf/model_hour.parquet` lazily. The only thing we materialise
in full is the target column, which is one f64 column over 11.9M rows.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from ..settings import REPO_ROOT

PANEL = REPO_ROOT / "data" / "bronze_sf" / "model_hour.parquet"
REPORT = REPO_ROOT / "docs" / "07_data_profile.md"

# Columns whose nulls are a fact about the design rather than a gap in the data, and the
# condition that explains them. Anything a predicate does not account for is a real gap and
# we report it as a residual. We separate the two because a bare null-rate table makes a
# correctly-built panel look 78% broken.
STRUCTURAL = {
    "first_pitch_hour": ("no game that day", ~pl.col("giants_home")),
    "day_night": ("no game that day", ~pl.col("giants_home")),
    "relative_hour": ("no game that day", ~pl.col("giants_home")),
}


def panel(path: Path | str | None = None) -> pl.LazyFrame:
    return pl.scan_parquet(path or PANEL)


def missingness(lf: pl.LazyFrame | None = None) -> pl.DataFrame:
    """Null count per column, split into the part a design predicate explains and the
    part that is a genuine gap."""
    lf = panel() if lf is None else lf
    cols = lf.collect_schema().names()
    total = lf.select(pl.len()).collect().item()
    nulls = lf.select([pl.col(c).null_count().alias(c) for c in cols]).collect()
    nulls = {c: nulls[c][0] for c in cols}

    explained = {}
    for col, (_, pred) in STRUCTURAL.items():
        if col in nulls:
            explained[col] = (
                lf.filter(pred & pl.col(col).is_null()).select(pl.len()).collect().item()
            )

    rows = []
    for c in cols:
        n = nulls[c]
        if not n:
            continue
        exp = explained.get(c, 0)
        rows.append(
            {
                "column": c,
                "n_null": n,
                "null_rate": n / total,
                "structural": exp,
                "gap": n - exp,
                "gap_rate": (n - exp) / total,
                # "no design predicate" states what we checked, not what we suppose the
                # cause is. The weather gaps are almost certainly holes in the station
                # record, but that is an interpretation and it belongs in the prose.
                "reason": STRUCTURAL[c][0] if c in STRUCTURAL else "no design predicate",
            }
        )
    return pl.DataFrame(rows).sort("n_null", descending=True)


def target(lf: pl.LazyFrame | None = None) -> dict:
    """Shape of `person_hours`, and what it looks like after log1p.

    Our panel is a dense spine, so every cell has a row for every hour whether or not any
    POI in it reported. The zeros are therefore mostly "nothing observed here this hour"
    rather than "no activity". We put the zero share next to the distribution rather than
    burying it, because it is a quarter of the rows and it is what makes the raw scale
    useless.
    """
    lf = panel() if lf is None else lf
    y = lf.select("person_hours").collect()["person_hours"]
    ly = y.log1p()
    return {
        "n": len(y),
        "zeros": int((y == 0).sum()),
        "zero_rate": float((y == 0).mean()),
        "mean": float(y.mean()),
        "median": float(y.median()),
        "max": float(y.max()),
        "q": {q: float(y.quantile(q)) for q in (0.25, 0.5, 0.75, 0.9, 0.99, 0.999)},
        "skew": float(y.skew()),
        "log_mean": float(ly.mean()),
        "log_median": float(ly.median()),
        "log_skew": float(ly.skew()),
        "raw": y,
        "log": ly,
    }


def splits(lf: pl.LazyFrame | None = None) -> pl.DataFrame:
    """Per-split target and coverage.

    Our split is temporal and the panel thins over time, so these rows are the check on
    whether our train, val and test metrics are even comparable.
    """
    lf = panel() if lf is None else lf
    return (
        lf.group_by("split")
        .agg(
            pl.col("date").min().alias("from"),
            pl.col("date").max().alias("to"),
            pl.len().alias("n_rows"),
            pl.col("person_hours").mean().alias("mean_ph"),
            pl.col("person_hours").log1p().mean().alias("mean_log_ph"),
            (pl.col("person_hours") == 0).mean().alias("zero_rate"),
            (pl.col("n_poi_reporting").sum() / pl.col("n_poi").sum()).alias("coverage"),
            pl.col("giants_home").mean().alias("game_hour_share"),
        )
        .sort("from")
        .collect()
    )


NUMERIC = [
    ("n_poi", "POIs in cell"),
    ("dist_venue_m", "Distance to venue"),
    ("food_share", "Food share of cell"),
    ("hour", "Hour of day"),
    ("t_index", "Days since start"),
    ("month", "Month"),
    ("dow", "Day of week"),
    ("tmax", "Max temperature"),
    ("prcp", "Precipitation (daily)"),
    ("temp_hr", "Temperature (hourly)"),
    ("wind_hr", "Wind (hourly)"),
]


def correlations(lf: pl.LazyFrame | None = None, n: int = 400_000, seed: int = 0) -> pl.DataFrame:
    """Correlation of each numeric covariate with the log target, raw and net of hour.

    We sample because a full 11.9M x 11 materialisation buys no precision that matters at
    two decimal places, and we seed it so the table does not move between runs. We drop
    nulls pairwise. `temp_hr` and `wind_hr` have real gaps, and a whole-frame drop would
    silently reweight every other column towards the hours those two cover.

    The second column is the one worth reading. Everything in this panel moves with the
    hour of day, so a raw correlation cannot tell a demand driver from a clock. We demean
    both sides within hour-of-day to take the diurnal cycle out and ask what is left.
    `temp_hr` goes from +0.15 to +0.01, which says the hourly temperature series is
    standing in for time of day and almost nothing else.
    """
    lf = panel() if lf is None else lf
    cols = [c for c, _ in NUMERIC]
    # `hour` is both a covariate in its own right and the control for every other one,
    # so it has to be de-duplicated out of the projection.
    df = (
        lf.select([*dict.fromkeys([*cols, "hour"]), "person_hours"])
        .collect(engine="streaming")
        .sample(n, seed=seed)
        .with_columns(pl.col("person_hours").log1p().alias("y"))
    )
    rows = []
    for col, label in NUMERIC:
        pair = df.select(*dict.fromkeys([col, "y", "hour"])).drop_nulls()
        net = None
        if col != "hour":  # undefined against itself
            dm = pair.with_columns(
                (pl.col(col) - pl.col(col).mean().over("hour")).alias("_x"),
                (pl.col("y") - pl.col("y").mean().over("hour")).alias("_y"),
            )
            net = dm.select(pl.corr("_x", "_y")).item()
        rows.append(
            {
                "column": col,
                "label": label,
                "r": pair.select(pl.corr(col, "y")).item(),
                "r_net_of_hour": net,
                "n": pair.height,
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("r").abs().alias("abs_r")).sort(
        "abs_r", descending=True
    )


def _md_table(df: pl.DataFrame, fmt: dict[str, str] | None = None) -> str:
    fmt = fmt or {}
    cols = df.columns
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.iter_rows(named=True):
        cells = []
        for c in cols:
            v = row[c]
            if v is None:  # e.g. hour-of-day has no correlation net of itself
                cells.append("—")
            else:
                cells.append(format(v, fmt[c]) if c in fmt else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def write_report(path: Path | None = None) -> Path:
    """Land the profile as markdown.

    We keep this file descriptive. It states what the numbers are, and what we should do
    about them belongs in docs/04 and the model report.
    """
    path = path or REPORT
    lf = panel()
    t = target(lf)
    miss = missingness(lf)
    sp = splits(lf)
    corr = correlations(lf)

    gaps = miss.filter(pl.col("gap") > 0)
    tr, te = sp.filter(pl.col("split") == "train"), sp.filter(pl.col("split") == "test")

    body = f"""# 07 — Data profile

Generated by `uv run python -m eia_pipeline.eda.profile`. It describes
`data/bronze_sf/model_hour.parquet` — the single table both of our model tiers read.
Figures 09 to 12 in `docs/reports/figures/` are these same numbers drawn.

We keep this file descriptive. What the numbers imply for our modelling lives in
`docs/04_data_exploration.md` and `docs/PIPELINE.md`.

## Shape

{t['n']:,} rows — 452 cells x 26,280 hours, a dense spine with no gaps in the index.

## The target

`person_hours`, raw:

| statistic | value |
|---|---|
| zero rows | {t['zeros']:,} ({t['zero_rate']:.1%}) |
| median | {t['median']:,.0f} |
| mean | {t['mean']:,.0f} |
| 90th pct | {t['q'][0.9]:,.0f} |
| 99th pct | {t['q'][0.99]:,.0f} |
| 99.9th pct | {t['q'][0.999]:,.0f} |
| max | {t['max']:,.0f} |
| skew | {t['skew']:.1f} |

A quarter of our panel is zero and the top thousandth is two orders of magnitude above the
median. Our spine is dense by construction — every cell gets a row every hour whether or
not any POI in it reported — so most of those zeros are "nothing observed here this hour"
rather than "no activity here". We model on `log1p(person_hours)`, which takes skew from
{t['skew']:.1f} to {t['log_skew']:.2f} — near-symmetric, with the zeros piled at the left
edge rather than spread across four orders of magnitude.

## Missingness

{_md_table(
    miss.select("column", "n_null", "null_rate", "structural", "gap", "reason"),
    {"n_null": ",", "null_rate": ".1%", "structural": ",", "gap": ","},
)}

The three large ones are structural: a row cannot carry a first-pitch hour on a day with
no game. `relative_hour` carries {int(gaps.filter(pl.col('column') == 'relative_hour')['gap'][0]):,}
nulls beyond that, all of them on 2024-07-27 — the one doubleheader in our window, where
two games share a date and a single relative-hour offset is undefined.

The weather columns are genuine gaps. `prcp_hr` is missing for
{miss.filter(pl.col('column') == 'prcp_hr')['null_rate'][0]:.1%} of rows, `temp_hr` and
`wind_hr` for about 0.5% each. These are hourly station series, so we read a gap as a gap
in the observation record rather than a design choice.

## Splits

{_md_table(
    sp.select("split", "from", "to", "n_rows", "mean_ph", "zero_rate", "coverage"),
    {"n_rows": ",", "mean_ph": ".1f", "zero_rate": ".1%", "coverage": ".1%"},
)}

Our split is temporal and the panel thins along the same axis, so the three are not drawn
from the same population. Mean person-hours falls from {tr['mean_ph'][0]:,.0f} in train to
{te['mean_ph'][0]:,.0f} in test, and the zero share rises from {tr['zero_rate'][0]:.1%} to
{te['zero_rate'][0]:.1%}. We are therefore measuring test-split error on a materially
sparser panel than we fitted on. This does not invalidate our comparison between models —
every model faces the same test set — but it does mean we should not read train-to-test
error movement as generalisation gap alone.

## Covariate correlation with the log target

{_md_table(
    corr.select(
        pl.col("label").alias("covariate"),
        pl.col("r"),
        pl.col("r_net_of_hour").alias("r net of hour"),
        pl.col("n"),
    ),
    {"r": "+.3f", "r net of hour": "+.3f", "n": ","},
)}

At cell-hour grain our panel is dominated by cross-sectional structure: how many POIs a
cell contains and how far it sits from the venue. Those two survive everything.

The second column demeans both sides within hour-of-day, which is the check that matters
here — every series in this table moves with the clock, so a raw correlation cannot
distinguish a demand driver from a proxy for time of day. `temp_hr` falls from
{corr.filter(pl.col('column') == 'temp_hr')['r'][0]:+.3f} to
{corr.filter(pl.col('column') == 'temp_hr')['r_net_of_hour'][0]:+.3f} and `wind_hr` from
{corr.filter(pl.col('column') == 'wind_hr')['r'][0]:+.3f} to
{corr.filter(pl.col('column') == 'wind_hr')['r_net_of_hour'][0]:+.3f}: the hourly weather
series are standing in for the diurnal cycle and carry almost nothing else at this grain.
Daily `tmax` and `prcp` correlate near zero either way, because they vary only across days
while most of the variance in this table is across cells.

None of this says weather is irrelevant. It says weather is not a cell-hour signal. Where
it matters to us is at day grain and in the treatment assignment, where our game days are
measurably warmer and drier than our control days (figure 04). That is a confounding
problem rather than a demand one.
"""
    path.write_text(body)
    return path


if __name__ == "__main__":
    print(f"wrote {write_report().relative_to(REPO_ROOT)}")
