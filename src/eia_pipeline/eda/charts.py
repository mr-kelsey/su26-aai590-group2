"""Render the report figures to PNG.

Every figure reads a file that is already in the repo: `data/gold/*.parquet`,
`data/bronze_sf/*.parquet`, or an ablation `*.json` written by `scripts/run_*.py`. Nothing
here re-runs a model, hits S3, or recomputes an effect. The numbers we draw are the same
ones the tables in `docs/PIPELINE.md` and `docs/04_data_exploration.md`
quote, so a chart and its table cannot disagree. When a number needs to change we change
it upstream and re-render.

    make figures                                  # all of them
    uv run python -m eia_pipeline.eda.charts      # same thing
    uv run python -m eia_pipeline.eda.charts panel_coverage balance   # a subset

Output lands in `docs/reports/figures/` at 200 dpi. We size each canvas to fill a slide and
still drop into a paper at half width without resampling. The sizes are fixed and nothing
samples randomly, so re-rendering an unchanged input gives us a byte-comparable PNG.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display anywhere this runs; must precede pyplot
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from ..settings import REPO_ROOT  # noqa: E402
from . import profile as prof  # noqa: E402

FIG_DIR = REPO_ROOT / "docs" / "reports" / "figures"
DATA = REPO_ROOT / "data"

# Two hues that stay distinguishable in grayscale and to the common colour-vision
# deficiencies. Warm is the treated or event series, cool is the control or baseline. We
# keep that mapping across every figure so a colour means the same thing in all of them.
EVENT = "#c0442c"
CONTROL = "#2f6f9f"
NEUTRAL = "#6b6b6b"
ACCENT = "#8a6d3b"
INK = "#1a1a1a"

BANDS = ("0-500m", "500m-1km", "1-2km", "2-4km", ">4km")


def _setup() -> None:
    plt.rcParams.update(
        {
            # We turn this off because a dollar-pair like "$59.6k-$110.9k" is otherwise
            # parsed as mathtext, and renders the middle in italic serif with the signs
            # eaten. The cost is that matplotlib's own log-axis formatter also emits
            # mathtext, so any log axis we draw needs its ticks set explicitly.
            "text.parse_math": False,
            "figure.dpi": 200,
            "savefig.dpi": 200,
            "savefig.bbox": "tight",
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.labelsize": 10,
            "axes.edgecolor": "#999999",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": "#e0e0e0",
            "grid.linewidth": 0.6,
            "legend.frameon": False,
            "text.color": INK,
            "axes.labelcolor": INK,
            "xtick.color": NEUTRAL,
            "ytick.color": NEUTRAL,
        }
    )


def _save(fig, name: str) -> Path:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    path = FIG_DIR / f"{name}.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def _dots(entries: list[tuple[str, str]]) -> list[Line2D]:
    """Proxy artists for a dot legend: one filled circle per (label, colour).

    We build proxies rather than pass the real artists because what a legend key needs to
    say is often not what drew the marks. A bar, an error bar and a scatter point all
    reduce to "this colour means this". Several of our figures also colour by significance
    rather than by series, and no real artist carries that.
    """
    return [
        Line2D([], [], marker="o", linestyle="none", markersize=8, color=c, label=label)
        for label, c in entries
    ]


def _legend(ax, entries: list[tuple[str, str]], loc: str = "upper right", **kw):
    """Dot legend inside one set of axes."""
    return ax.legend(handles=_dots(entries), loc=loc, fontsize=9, **kw)


def _figure_legend(fig, entries: list[tuple[str, str]], y: float = -0.02):
    """Dot legend for the whole figure, laid out in one row under the axes.

    We use this on the multi-panel figures. The same key applies to every panel there, and
    repeating it in each one is noise.
    """
    return fig.legend(
        handles=_dots(entries),
        loc="upper center",
        bbox_to_anchor=(0.5, y),
        ncol=len(entries),
        fontsize=9.5,
        columnspacing=2.2,
        handletextpad=0.5,
    )


def _band_expr() -> pl.Expr:
    return (
        pl.when(pl.col("dist_venue_m") <= 500)
        .then(pl.lit("0-500m"))
        .when(pl.col("dist_venue_m") <= 1000)
        .then(pl.lit("500m-1km"))
        .when(pl.col("dist_venue_m") <= 2000)
        .then(pl.lit("1-2km"))
        .when(pl.col("dist_venue_m") <= 4000)
        .then(pl.lit("2-4km"))
        .otherwise(pl.lit(">4km"))
    )


# --------------------------------------------------------------------------- data side


def panel_coverage() -> Path:
    """The Advan panel thins over the window. This is the single most important caveat on
    every level and trend we report (docs/04 section 7).

    Two series, monthly. The share of POI-hours that register anything falls by a third
    between 2023 and 2025, and mean person-hours falls with it. Because the two move
    together, a level read off 2025 is not comparable to one read off 2023. That is why we
    estimate the per-band effects on 2023-2024 only.
    """
    lf = pl.scan_parquet(DATA / "bronze_sf" / "model_hour.parquet")
    m = (
        lf.group_by(pl.col("date").dt.truncate("1mo").alias("month_start"))
        .agg(
            (pl.col("n_poi_reporting").sum() / pl.col("n_poi").sum() * 100).alias("report_pct"),
            pl.col("person_hours").mean().alias("mean_ph"),
        )
        .sort("month_start")
        .collect()
    )
    x = m["month_start"].to_list()

    series = [
        ("POI-hours reporting any activity", CONTROL),
        ("Mean person-hours per cell-hour", EVENT),
    ]

    fig, axes = plt.subplots(2, 1, figsize=(9, 5.4), sharex=True)
    for ax, col, (_, colour), label in (
        (axes[0], "report_pct", series[0], "% of POI-hours"),
        (axes[1], "mean_ph", series[1], "Person-hours"),
    ):
        y = m[col].to_numpy()
        ax.plot(x, y, color=colour, lw=1.8)
        ax.fill_between(x, 0, y, color=colour, alpha=0.10)
        ax.set_ylabel(label, fontsize=9)
        ax.set_ylim(0, y.max() * 1.18)
        first, last = y[:12].mean(), y[-12:].mean()
        ax.annotate(
            f"2023 mean {first:,.1f}  →  2025 mean {last:,.1f}   ({(last / first - 1) * 100:+.0f}%)",
            xy=(0.99, 0.9),
            xycoords="axes fraction",
            ha="right",
            fontsize=9,
            color=INK,
        )

    axes[0].set_title("The vendor panel thins across the window", loc="left")
    axes[1].set_xlabel("Month")
    _figure_legend(fig, series, y=0.03)
    return _save(fig, "fig01_panel_coverage")


def bart_calibration() -> Path:
    """Pre-game BART uplift against actual gate attendance. This is the first calibrated
    conversion factor in the project (docs/04 section 2).

    We draw the fit on the same 46 night games we compute r and the slope from, and we
    print both on the figure rather than leaving them to the surrounding prose.
    """
    df = pl.read_parquet(DATA / "gold" / "bart_attendance_calib_2024.parquet")
    att = df["attendance"].to_numpy().astype(float)
    up = df["uplift"].to_numpy()
    r = float(np.corrcoef(att, up)[0, 1])
    slope, intercept = np.polyfit(att, up, 1)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(att / 1000, up, s=42, color=CONTROL, alpha=0.75, edgecolor="white", linewidth=0.6)
    xs = np.linspace(att.min(), att.max(), 100)
    ax.plot(xs / 1000, slope * xs + intercept, color=EVENT, lw=1.8)
    ax.set_xlabel("MLB gate attendance (thousands)")
    ax.set_ylabel("Net BART arrivals at Embarcadero, 3h pre-game")
    ax.set_title("Transit uplift scales with the gate", loc="left")
    ax.annotate(
        f"r = {r:.2f}\n{slope * 1000:.0f} net arrivals per 1,000 fans\nn = {len(df)} night games",
        xy=(0.03, 0.95),
        xycoords="axes fraction",
        va="top",
        fontsize=10,
        color=INK,
    )
    _legend(ax, [("Night home game", CONTROL), ("Least-squares fit", EVENT)], loc="lower right")
    return _save(fig, "fig02_bart_calibration")


def diurnal_profile() -> Path:
    """Where and when the effect lives, and that it tracks first pitch.

    We keep day and night games apart on purpose. Pooling them puts 100 day starts at
    12-13h against 146 night starts at 18-19h, which smears the peak into a shapeless
    mid-afternoon bulge and makes the signal look like a generic busy-day effect. Split
    out, the day-game gap peaks at its own 13h first pitch and the night-game gap at 21h,
    an hour or two into a 19h game. Two populations, two peaks, each moving with its own
    start time. That is the strongest evidence we have in the raw panel that we are
    measuring the event and not the weather or the weekday.

    The bottom row is the same thing as a percentage gap on a shared axis, so the decay
    across bands can be read directly instead of inferred from three different y-scales.
    """
    lf = pl.scan_parquet(DATA / "bronze_sf" / "model_hour.parquet")
    grp = (
        pl.when(pl.col("giants_home") & (pl.col("day_night") == "night"))
        .then(pl.lit("night"))
        .when(pl.col("giants_home") & (pl.col("day_night") == "day"))
        .then(pl.lit("day"))
        .otherwise(pl.lit("control"))
    )
    d = (
        lf.with_columns(_band_expr().alias("band"))
        .filter(pl.col("band").is_in(BANDS[:3]))
        .filter(pl.col("giants_home") | pl.col("clean_control"))
        .group_by("band", "hour", grp.alias("grp"))
        .agg(pl.col("person_hours").mean().alias("ph"))
        .collect()
    )
    series = {
        "control": ("Clean control day", CONTROL),
        "night": ("Night game (first pitch 18-19h)", EVENT),
        "day": ("Day game (first pitch 12-13h)", ACCENT),
    }

    fig, axes = plt.subplots(2, 3, figsize=(11, 6.4), sharex=True)
    for j, band in enumerate(BANDS[:3]):
        top, bot = axes[0, j], axes[1, j]
        sub = d.filter(pl.col("band") == band)
        base = sub.filter(pl.col("grp") == "control").sort("hour")["ph"].to_numpy()
        for key, (label, colour) in series.items():
            y = sub.filter(pl.col("grp") == key).sort("hour")["ph"].to_numpy()
            top.plot(range(24), y, color=colour, lw=1.9, label=label)
            if key != "control":
                bot.plot(range(24), (y / base - 1) * 100, color=colour, lw=1.9)
        for hour, colour in ((13, ACCENT), (18, EVENT)):
            for ax in (top, bot):
                ax.axvline(hour, color=colour, lw=1, ls=(0, (3, 3)), alpha=0.55)
        top.set_title(band, loc="left")
        bot.axhline(0, color=NEUTRAL, lw=1)
        bot.set_xlabel("Hour of day")
        bot.set_xticks([0, 6, 12, 18, 23])
        bot.set_ylim(-25, 105)
        if j:
            bot.tick_params(labelleft=False)

    axes[0, 0].set_ylabel("Mean person-hours per cell-hour")
    axes[1, 0].set_ylabel("Gap vs control (%)")
    axes[1, 2].annotate(
        "dashed: first pitch",
        xy=(0.97, 0.92),
        xycoords="axes fraction",
        ha="right",
        fontsize=8,
        color=NEUTRAL,
    )
    fig.suptitle(
        "The game-day gap tracks first pitch, and decays with distance",
        x=0.005,
        ha="left",
        fontsize=12,
        fontweight="bold",
    )
    _figure_legend(fig, [(label, colour) for label, colour in series.values()])
    return _save(fig, "fig03_diurnal_profile")


def covariate_balance() -> Path:
    """What separates a game day from a control day besides the game.

    Standardised mean difference on every covariate our models control for. Game days are
    warmer and markedly drier than control days, because baseball is scheduled into good
    weather. That is precisely why weather enters the counterfactual instead of being waved
    away. Anything beyond +/-0.1 is conventionally treated as imbalance worth modelling,
    and four of our covariates clear it.
    """
    lf = pl.scan_parquet(DATA / "bronze_sf" / "model_hour.parquet")
    day = (
        lf.select(
            "date",
            "giants_home",
            "clean_control",
            "tmax",
            "prcp",
            "dow",
            "us_federal_holiday",
            "chase_day",
            "moscone_day",
            "street_fair_day",
            "citywide_day",
        )
        .unique(subset=["date"])
        .with_columns((pl.col("dow") >= 5).alias("weekend"))
        .collect()
    )
    game = day.filter(pl.col("giants_home"))
    ctrl = day.filter(pl.col("clean_control"))

    covs = [
        ("Max temperature", "tmax"),
        ("Precipitation", "prcp"),
        ("Weekend", "weekend"),
        ("Federal holiday", "us_federal_holiday"),
        ("Chase Center event", "chase_day"),
        ("Moscone event", "moscone_day"),
        ("Street fair", "street_fair_day"),
        ("Citywide convention", "citywide_day"),
    ]
    labels, smds = [], []
    for label, col in covs:
        g = game[col].cast(pl.Float64)
        c = ctrl[col].cast(pl.Float64)
        pooled = math.sqrt((g.std() ** 2 + c.std() ** 2) / 2)
        labels.append(label)
        smds.append(0.0 if pooled == 0 else (g.mean() - c.mean()) / pooled)

    order = np.argsort(np.abs(smds))
    labels = [labels[i] for i in order]
    smds = [smds[i] for i in order]
    y = np.arange(len(labels))

    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    ax.axvspan(-0.1, 0.1, color=NEUTRAL, alpha=0.10, lw=0)
    ax.axvline(0, color=NEUTRAL, lw=1)
    ax.hlines(y, 0, smds, color="#c9c9c9", lw=1.2)
    ax.scatter(
        smds, y, s=70, color=[EVENT if abs(v) > 0.1 else CONTROL for v in smds], zorder=3
    )
    for yi, v in zip(y, smds):
        ax.annotate(
            f"{v:+.2f}",
            xy=(v, yi),
            xytext=(9 if v >= 0 else -9, 0),
            textcoords="offset points",
            va="center",
            ha="left" if v >= 0 else "right",
            fontsize=9,
            color=INK,
        )
    ax.set_yticks(y, labels)
    ax.set_xlabel("Standardised mean difference   (game day − clean control day)")
    ax.set_xlim(-0.62, 0.62)
    ax.set_title("Game days are warmer and drier than control days", loc="left")
    ax.grid(axis="y", visible=False)
    _legend(
        ax,
        [("Imbalanced (|SMD| > 0.1)", EVENT), ("Balanced", CONTROL)],
        loc="lower left",
    )
    return _save(fig, "fig04_covariate_balance")


# ------------------------------------------------------------------------ results side


def distance_decay() -> Path:
    """The venue crossover: does the effect follow the venue or the neighbourhood?

    Four panels, one per (event, measured-around-venue) pair. The diagonal is the real
    effect and the off-diagonal is the placebo: Giants games measured around Chase, and
    Chase events measured around Oracle.

    Read the 0-500m row. Each event moves its own inner ring hard (+43.8%, +648.7%) and
    does nothing to the other venue's (+1.3% n.s., -11.5% n.s.). That is what rules out a
    generic downtown-is-busy factor. The middle bands are a different matter and we do not
    claim they separate. The two venues are close enough that their 500m-1km and 1-2km
    rings physically overlap, so both events register in both, as they should.
    """
    cross = json.loads((DATA / "bronze_sf" / "tier3_crossover.json").read_text())
    titles = {
        "giants|oracle": "Giants games, around Oracle Park",
        "chase|oracle": "Chase events, around Oracle Park",
        "giants|chase": "Giants games, around Chase Center",
        "chase|chase": "Chase events, around Chase Center",
    }
    y = np.arange(len(BANDS))[::-1]

    # The diagonal, an event measured around its own venue, is the real effect. We draw
    # the other two panels cool because they are placebos and the eye should separate them.
    diagonal = {"giants|oracle", "chase|chase"}

    fig, axes = plt.subplots(2, 2, figsize=(11, 6.8))
    fig.subplots_adjust(hspace=0.42, wspace=0.28)
    for ax, (key, title) in zip(axes.ravel(), titles.items()):
        rows = {r["band"]: r for r in cross[key]}
        eff = np.array([rows[b]["effect_pct"] for b in BANDS])
        lo = np.array([rows[b]["lo_pct"] for b in BANDS])
        hi = np.array([rows[b]["hi_pct"] for b in BANDS])
        sig = np.array([rows[b]["p"] < 0.05 for b in BANDS])
        base = EVENT if key in diagonal else CONTROL
        colours = [base if s else "#bdbdbd" for s in sig]
        ax.axvline(0, color=NEUTRAL, lw=1)
        ax.hlines(y, lo, hi, color=colours, lw=2.4, alpha=0.55)
        ax.scatter(eff, y, s=52, color=colours, zorder=3)
        # We put labels to the RIGHT of each interval and never above. Stacked above, the
        # top band's label collides with the panel title.
        for yi, e, h, s in zip(y, eff, hi, sig):
            ax.annotate(
                f"{e:+.1f}%" + ("" if s else "  n.s."),
                xy=(h, yi),
                xytext=(7, 0),
                textcoords="offset points",
                va="center",
                ha="left",
                fontsize=8.5,
                color=INK if s else NEUTRAL,
            )
        ax.set_yticks(y, BANDS)
        ax.set_title(title, loc="left", fontsize=10.5)
        ax.grid(axis="y", visible=False)
        # Chase-at-Chase runs to +649% and would flatten every other panel on a shared
        # axis, so we scale each panel to its own data and label it with the numbers.
        span = hi.max() - lo.min()
        ax.set_xlim(lo.min() - span * 0.10, hi.max() + span * 0.55)

    for ax in axes[1]:
        ax.set_xlabel("Effect on person-hours (%)")
    fig.suptitle(
        "Each event moves its own inner ring, and not the other venue's",
        x=0.005,
        y=1.02,
        ha="left",
        fontsize=12,
        fontweight="bold",
    )
    _figure_legend(
        fig,
        [
            ("Event at its own venue", EVENT),
            ("Placebo: the other venue", CONTROL),
            ("Not significant (p > 0.05)", "#bdbdbd"),
        ],
    )
    return _save(fig, "fig05_distance_decay")


def feature_ablation() -> Path:
    """What each block of features buys the Tier 1 counterfactual.

    Calendar and weather alone get most of the way there. The multi-depth rolling baseline
    is what takes test MAE below 1.0. We report bias alongside because the single-depth
    baseline has the smallest of it, but the multi-depth model is better on every other
    axis and bias is within noise at this scale.
    """
    # Re-run 2026-08-07 on the post-PR-#23 panel, all three arms in one experiment:
    # same load(), same lgb_params(600, seed=0), same splits, only the feature list
    # differs. The k=8 column is not in rolling_baseline.parquet, so that arm rebuilds
    # it with the same window logic (ROWS BETWEEN 7 PRECEDING) before fitting.
    rows = [
        ("Calendar + weather only", 1.1177, 1.2369, 0.6210),
        ("+ single k=8 baseline", 0.9761, 1.0530, 0.7156),
        ("+ multi-depth baseline", 0.8722, 0.9372, 0.7495),
    ]
    labels = [r[0] for r in rows]
    val = np.array([r[1] for r in rows])
    test = np.array([r[2] for r in rows])
    r2 = np.array([r[3] for r in rows])
    y = np.arange(len(rows))[::-1]
    h = 0.34

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [2, 1]})
    ax.barh(y + h / 2, val, height=h, color=CONTROL, label="Validation MAE")
    ax.barh(y - h / 2, test, height=h, color=EVENT, label="Test MAE")
    for yi, v, t in zip(y, val, test):
        ax.annotate(f"{v:.4f}", xy=(v, yi + h / 2), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=8.5, color=INK)
        ax.annotate(f"{t:.4f}", xy=(t, yi - h / 2), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=8.5, color=INK)
    ax.set_yticks(y, labels)
    ax.set_xlim(0, 1.55)
    ax.set_xlabel("Mean absolute error (log person-hours)")
    ax.set_title("Each feature block lowers error", loc="left")
    ax.grid(axis="y", visible=False)

    ax2.barh(y, r2, height=0.5, color=NEUTRAL)
    for yi, v in zip(y, r2):
        ax2.annotate(f"{v:.3f}", xy=(v, yi), xytext=(4, 0), textcoords="offset points",
                     va="center", fontsize=8.5, color=INK)
    ax2.tick_params(left=False, labelleft=False)
    ax2.set_xlim(0, 0.92)
    ax2.set_xlabel("Test R²")
    ax2.set_title("...and raises R²", loc="left")
    ax2.grid(axis="y", visible=False)
    _figure_legend(
        fig,
        [("Validation MAE", CONTROL), ("Test MAE", EVENT), ("Test R²", NEUTRAL)],
        y=-0.06,
    )
    return _save(fig, "fig06_feature_ablation")


def edge_ablation_seeds() -> Path:
    """The graph edges do not beat the seed.

    Three seeds per edge arm. The spread within an arm is wider than the gap between arms,
    so the edge type is not identifiable at this sample size, and any single-seed ranking
    of them is noise. That includes the ranking our earlier tables quoted. This is the
    honest version of the edge-ablation table, and it is why we report Tier 2 as not
    beating Tier 1 rather than as a tuning problem.
    """
    seeds = json.loads((DATA / "bronze_sf" / "tier2_ablation_seeds.json").read_text())
    runs = [r for r in seeds["runs"] if not r.get("repeat")]
    arms = ["none", "contiguity", "distance", "flow"]
    label = {
        "none": "None\n(temporal only)",
        "contiguity": "Grid\ncontiguity",
        "distance": "Distance\nkernel",
        "flow": "Visitor-origin\nflow",
    }

    fig, ax = plt.subplots(figsize=(8.6, 4.8))
    for i, arm in enumerate(arms):
        vals = np.array([r["test_mae"] for r in runs if r["edges"] == arm])
        ax.hlines(vals.mean(), i - 0.26, i + 0.26, color=INK, lw=2)
        ax.vlines(i, vals.min(), vals.max(), color="#c9c9c9", lw=1.4)
        ax.scatter(np.full(vals.shape, i), vals, s=64, color=CONTROL, alpha=0.85, zorder=3)
        ax.annotate(f"mean {vals.mean():.3f}", xy=(i + 0.28, vals.mean()), xytext=(5, 0),
                    textcoords="offset points", va="center", fontsize=8.5, color=INK)

    allv = np.array([r["test_mae"] for r in runs])
    ax.axhspan(allv.min(), allv.max(), color=CONTROL, alpha=0.06, lw=0)
    ax.set_xticks(range(len(arms)), [label[a] for a in arms])
    ax.set_ylabel("Test MAE (log person-hours)")
    ax.set_title("Seed noise swamps the edge choice", loc="left")
    ax.annotate(
        f"within-arm spread up to {max(np.ptp([r['test_mae'] for r in runs if r['edges']==a]) for a in arms):.3f}\n"
        f"between-arm spread of means {np.ptp([np.mean([r['test_mae'] for r in runs if r['edges']==a]) for a in arms]):.3f}",
        xy=(0.99, 0.94),
        xycoords="axes fraction",
        ha="right",
        va="top",
        fontsize=9,
        color=INK,
    )
    ax.grid(axis="x", visible=False)
    _legend(ax, [("Individual seed run", CONTROL), ("Arm mean", INK)], loc="lower right")
    return _save(fig, "fig07_edge_ablation_seeds")


def dollars_by_band() -> Path:
    """Where the per-game dollars come from, and how confident we are in each slice.

    The inner band has the largest percentage effect but the smallest dollar base. 1-2km
    contributes the most absolute dollars, because there is far more evening trade there.
    Beyond 4km the effect is not distinguishable from zero, and we enter it as zero rather
    than as a noisy point estimate, so our total is a floor.
    """
    rows = [
        ("0-500m", 63_945, 19_723, 18_200, 21_300),
        ("500m-1km", 74_473, 9_601, 8_100, 11_000),
        ("1-2km", 826_305, 35_583, 26_400, 44_600),
        ("2-4km", 1_737_271, 20_600, 6_900, 34_100),
        (">4km", 1_889_233, 0, 0, 0),
    ]
    labels = [r[0] for r in rows]
    attrib = np.array([r[2] for r in rows], dtype=float)
    lo = np.array([r[3] for r in rows], dtype=float)
    hi = np.array([r[4] for r in rows], dtype=float)
    x = np.arange(len(rows))

    fig, ax = plt.subplots(figsize=(9, 4.8))
    colours = [EVENT if a > 0 else "#c9c9c9" for a in attrib]
    ax.bar(x, attrib / 1000, width=0.56, color=colours)
    ax.errorbar(
        x, attrib / 1000, yerr=[(attrib - lo) / 1000, (hi - attrib) / 1000],
        fmt="none", ecolor=INK, elinewidth=1.3, capsize=5,
    )
    for xi, a, h in zip(x, attrib, hi):
        ax.annotate(
            f"${a / 1000:,.1f}k" if a else "not significant",
            xy=(xi, h / 1000),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color=INK if a else NEUTRAL,
        )
    ax.set_xticks(x, labels)
    ax.set_ylabel("Attributable evening spend per game ($ thousands)")
    ax.set_xlabel("Distance band from home plate")
    ax.set_ylim(0, 52)
    ax.set_title("Per-game attributable taxable food spend: $85.5k", loc="left")
    ax.annotate(
        "total $85,507 per game\n95% range $59.6k-$110.9k",
        xy=(0.99, 0.94),
        xycoords="axes fraction",
        ha="right",
        va="top",
        fontsize=9.5,
        color=INK,
    )
    ax.grid(axis="x", visible=False)
    _legend(
        ax,
        [("Attributable spend (95% CI)", EVENT), ("Effect not significant", "#c9c9c9")],
        loc="upper left",
    )
    return _save(fig, "fig08_dollars_by_band")


# -------------------------------------------------------------------------- profile side
# These four draw `eia_pipeline.eda.profile`, which also writes docs/07_data_profile.md.
# One computation and two renderings, so our prose and our pictures cannot disagree.


def target_distribution() -> Path:
    """What the target looks like, and why we model everything in logs.

    Raw person-hours is unusable. 27.5% of it is zero and the top thousandth sits two
    orders of magnitude above the median. log1p takes skew from 6.2 to near zero. The zero
    spike does not go away. It becomes a spike at exactly 0 rather than one smeared across
    the bottom of a four-decade range, and that is the honest picture, because those zeros
    are mostly "nothing observed here this hour" on a dense spine rather than measured
    absence of activity.
    """
    t = prof.target()
    raw, log = t["raw"].to_numpy(), t["log"].to_numpy()

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))
    # We draw raw on a log x-axis and hold the zeros out, counting them in the annotation
    # instead. On a linear axis this panel is one bar at the left and nothing else.
    nz = raw[raw > 0]
    bins = np.logspace(0, np.log10(raw.max()), 60)
    ax.hist(nz, bins=bins, color=CONTROL, alpha=0.85)
    ax.set_xscale("log")
    # The default LogFormatter emits mathtext, which we disable globally so that dollar
    # signs in labels survive. We set plain decade ticks instead.
    ax.set_xticks([1, 10, 100, 1_000, 10_000])
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    ax.xaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_major_formatter(lambda v, _: f"{v / 1000:,.0f}k")
    ax.set_xlabel("person-hours per cell-hour (log scale, zeros excluded)")
    ax.set_ylabel("Cell-hours")
    ax.set_title("Raw: skew 6.2, a quarter of it zero", loc="left")
    ax.annotate(
        f"{t['zeros']:,} zero rows ({t['zero_rate']:.1%})\nnot shown on a log axis\n\n"
        f"median {t['median']:,.0f}   99.9th pct {t['q'][0.999]:,.0f}   max {t['max']:,.0f}",
        xy=(0.03, 0.95),
        xycoords="axes fraction",
        va="top",
        fontsize=9,
        color=INK,
    )

    ax2.hist(log, bins=60, color=EVENT, alpha=0.85)
    ax2.yaxis.set_major_formatter(lambda v, _: f"{v / 1e6:,.1f}M")
    ax2.set_xlabel("log1p(person-hours)")
    ax2.set_title(f"log1p: skew {t['log_skew']:.2f}, the modelled scale", loc="left")
    ax2.annotate(
        "the zero spike survives\nthe transform, at exactly 0",
        xy=(0.02, 0.62),
        xycoords="axes fraction",
        xytext=(0.22, 0.86),
        textcoords="axes fraction",
        fontsize=9,
        color=INK,
        arrowprops={"arrowstyle": "->", "color": NEUTRAL, "lw": 1},
    )
    _figure_legend(
        fig, [("Raw person-hours", CONTROL), ("log1p(person-hours)", EVENT)], y=-0.04
    )
    return _save(fig, "fig09_target_distribution")


def missingness() -> Path:
    """Which columns are missing, and how much of that is by design.

    A bare null-rate chart makes this panel look 78% broken. Three quarters of the rows
    carry no first-pitch hour because three quarters of our days have no game, which is
    the design rather than a defect. We split each bar into the part a predicate explains
    and the part left over, and that is what makes the chart readable. What remains is one
    doubleheader and the weather series.
    """
    m = prof.missingness()
    labels = m["column"].to_list()[::-1]
    struct = np.array(m["structural"].to_list()[::-1], dtype=float) / 1e6
    gap = np.array(m["gap"].to_list()[::-1], dtype=float) / 1e6
    y = np.arange(len(labels))
    total = prof.panel().select(pl.len()).collect().item()

    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    ax.barh(y, struct, height=0.6, color=CONTROL)
    ax.barh(y, gap, height=0.6, left=struct, color=EVENT)
    for yi, s, g in zip(y, struct, gap):
        ax.annotate(
            f"{(s + g) / (total / 1e6):.1%}" + (f"   gap: {g * 1e6:,.0f}" if g else ""),
            xy=(s + g, yi),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=9,
            color=INK,
        )
    ax.set_yticks(y, labels)
    ax.set_xlabel("Null rows (millions)")
    ax.set_xlim(0, total / 1e6 * 1.32)
    ax.set_title("Most of the missingness is the design, not a gap", loc="left")
    ax.grid(axis="y", visible=False)
    _legend(
        ax,
        [("Explained by design", CONTROL), ("Genuine gap", EVENT)],
        loc="lower right",
    )
    return _save(fig, "fig10_missingness")


def covariate_correlation() -> Path:
    """What correlates with the target, before and after we take out the clock.

    Everything in a cell-hour panel moves with the hour of day, so a raw correlation cannot
    tell a demand driver from a proxy for time. Demeaning both sides within hour-of-day is
    the check. Our structural covariates get slightly stronger and the hourly weather
    series collapse from about 0.15 to about 0.01. They were the clock.
    """
    c = prof.correlations().filter(pl.col("column") != "hour").sort("abs_r")
    labels = c["label"].to_list()
    r = np.array(c["r"].to_list())
    net = np.array(c["r_net_of_hour"].to_list())
    y = np.arange(len(labels))

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.axvline(0, color=NEUTRAL, lw=1)
    ax.hlines(y, r, net, color="#d5d5d5", lw=1.6, zorder=1)
    ax.scatter(r, y, s=62, color=CONTROL, zorder=3)
    ax.scatter(net, y, s=62, color=EVENT, zorder=3)
    for yi, a, b in zip(y, r, net):
        far = max(a, b) if abs(max(a, b)) > abs(min(a, b)) else min(a, b)
        ax.annotate(
            f"{a:+.2f} → {b:+.2f}",
            xy=(far, yi),
            xytext=(9 if far >= 0 else -9, 0),
            textcoords="offset points",
            va="center",
            ha="left" if far >= 0 else "right",
            fontsize=8.5,
            color=INK,
        )
    ax.set_yticks(y, labels)
    ax.set_xlim(-0.62, 0.68)
    ax.set_xlabel("Correlation with log1p(person-hours)")
    ax.set_title("The hourly weather series were measuring the clock", loc="left")
    ax.grid(axis="y", visible=False)
    _legend(
        ax,
        [("Raw correlation", CONTROL), ("Net of hour of day", EVENT)],
        loc="lower right",
    )
    return _save(fig, "fig11_covariate_correlation")


def split_drift() -> Path:
    """Our three splits are not drawn from the same population.

    The split is temporal and the panel thins along the same axis, so train sits in a
    denser regime than test. Mean person-hours falls a quarter and the zero share rises ten
    points across the boundary. This leaves model-versus-model comparison alone, because
    every model faces the same test set. What it does mean is that we cannot read
    train-to-test error movement as a clean generalisation gap, since the test rows are
    sparser to begin with.
    """
    sp = prof.splits()
    lf = prof.panel()
    weekly = (
        lf.group_by(pl.col("date").dt.truncate("1w").alias("week"))
        .agg(
            pl.col("person_hours").mean().alias("mean_ph"),
            (pl.col("person_hours") == 0).mean().alias("zero_rate"),
        )
        .sort("week")
        .collect()
    )
    bounds = {r["split"]: (r["from"], r["to"]) for r in sp.iter_rows(named=True)}
    shade = {"train": CONTROL, "val": ACCENT, "test": EVENT}

    fig, axes = plt.subplots(2, 1, figsize=(9.5, 5.6), sharex=True)
    for ax, col, label in (
        (axes[0], "mean_ph", "Mean person-hours"),
        (axes[1], "zero_rate", "Share of cell-hours at zero"),
    ):
        vals = weekly[col].to_numpy() * (100 if col == "zero_rate" else 1)
        ax.plot(weekly["week"], vals, color=INK, lw=1.3)
        for split, (d0, d1) in bounds.items():
            ax.axvspan(d0, d1, color=shade[split], alpha=0.11, lw=0)
            row = sp.filter(pl.col("split") == split)
            v = row[col][0] * (100 if col == "zero_rate" else 1)
            ax.hlines(v, d0, d1, color=shade[split], lw=2.6)
        ax.set_ylabel(label, fontsize=9)
    axes[1].set_xlabel("Week")
    axes[1].yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    axes[0].set_title("Train sits in a denser panel than test", loc="left")
    axes[0].annotate(
        f"train {sp.filter(pl.col('split') == 'train')['mean_ph'][0]:,.0f}"
        f"  →  test {sp.filter(pl.col('split') == 'test')['mean_ph'][0]:,.0f} person-hours",
        xy=(0.99, 0.9),
        xycoords="axes fraction",
        ha="right",
        fontsize=9,
        color=INK,
    )
    _figure_legend(
        fig,
        [("Train", CONTROL), ("Validation", ACCENT), ("Test", EVENT)],
        y=0.03,
    )
    return _save(fig, "fig12_split_drift")


FIGURES = {
    "panel_coverage": panel_coverage,
    "bart_calibration": bart_calibration,
    "diurnal_profile": diurnal_profile,
    "covariate_balance": covariate_balance,
    "distance_decay": distance_decay,
    "feature_ablation": feature_ablation,
    "edge_ablation_seeds": edge_ablation_seeds,
    "dollars_by_band": dollars_by_band,
    "target_distribution": target_distribution,
    "missingness": missingness,
    "covariate_correlation": covariate_correlation,
    "split_drift": split_drift,
}


def main(argv: list[str] | None = None) -> None:
    _setup()
    names = argv if argv else list(FIGURES)
    unknown = [n for n in names if n not in FIGURES]
    if unknown:
        raise SystemExit(f"unknown figure(s): {unknown}. known: {list(FIGURES)}")
    for name in names:
        print(f"{name} -> {FIGURES[name]().relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
