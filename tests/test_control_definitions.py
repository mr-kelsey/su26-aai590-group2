"""`clean_control_strict` must be at least as strict as the shipped flag.

Both the training panel (`transform/features.py`) and the serving spine
(`serve/spine_2026.py`) build their own copy of this predicate, and the deployed
model's rolling baselines filter on it. The two copies have to agree, and both
have to be a subset of gold's `clean_control`, which already drops days with a
ballpark event. A concert or a Monster Jam at Oracle Park is not a control day
for a Giants game.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "eia_pipeline"
FLAGS = ["giants_home", "ballpark_day", "chase_day", "moscone_day",
         "citywide_day", "street_fair_day"]


def _predicate(path: Path) -> str:
    """The clean_control_strict expression as written in the file."""
    text = path.read_text()
    m = re.search(r"\(\s*(NOT [^)]*?)\)\s*\n?\s*AS clean_control_strict", text)
    assert m, f"no clean_control_strict expression found in {path.name}"
    return re.sub(r"\bt\.", "", " ".join(m.group(1).split()))


@pytest.fixture()
def con():
    c = duckdb.connect()
    yield c
    c.close()


def _evaluate(con, predicate: str, **flags) -> bool:
    cols = ", ".join(f"{'TRUE' if flags.get(f) else 'FALSE'} AS {f}" for f in FLAGS)
    return con.sql(f"SELECT ({predicate}) FROM (SELECT {cols})").fetchone()[0]


@pytest.mark.parametrize("rel", [
    "transform/features.py",
    "serve/spine_2026.py",
])
def test_ballpark_event_days_are_not_strict_controls(con, rel):
    """A non-Giants event at the ballpark draws a crowd to the same blocks the
    effect is measured on. Gold's own `clean_control` excludes these; strict
    dropped the exclusion, so 10 of the 388 control days in the 2023-2024 effect
    window were ballpark event days, running +123% on evening activity.
    """
    pred = _predicate(SRC / rel)
    assert _evaluate(con, pred, ballpark_day=True) is False


@pytest.mark.parametrize("rel", [
    "transform/features.py",
    "serve/spine_2026.py",
])
def test_a_quiet_day_is_still_a_strict_control(con, rel):
    """The exclusion must not swallow the control pool itself."""
    pred = _predicate(SRC / rel)
    assert _evaluate(con, pred) is True


def test_both_copies_of_the_predicate_agree():
    """features.py builds the training panel and spine_2026.py builds the serving
    spine. If they drift, the model is fitted on one control pool and served
    against another, and nothing raises."""
    a = _predicate(SRC / "transform/features.py")
    b = _predicate(SRC / "serve/spine_2026.py")
    assert a == b
