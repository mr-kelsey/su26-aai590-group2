"""Fit the model that gets DEPLOYED, and cache the booster to disk.

Two models come out of this repo with the same specification, and it matters
which one ships:

  tier1_gbm.fit()                  train split only, early-stopped on val.
                                   This is the EVIDENCE: test MAE 0.9185,
                                   R2 0.7568 on genuinely held-out control hours.
  effects.fit_full_control_model() every strict-control hour, all splits, all 600
                                   trees. This is the INSTRUMENT.

We deploy the second. `nowcast/effects.py` already argues why in its own
docstring: it still never sees a treated hour in any split, so its residuals stay
causally readable, but it is far better calibrated across the window, and the
window carries a 27% panel slide that the serve path then has to extrapolate
through. The model card cites the first model's metrics as the quality evidence
and says plainly that the artifact is the same specification refit on everything.

Note the two differ in size as well as data: the train-only fit early-stops
around iteration 111, while the full-control fit runs all 600 trees.

The fit is slow (minutes, 5.3M rows), so the booster is cached under
data/bronze_sf/. Delete the file to force a refit.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..settings import settings

MODEL_TXT = "served_model.txt"
MODEL_META = "served_model.meta.json"


def cache_dir() -> Path:
    return settings.data_dir / "bronze_sf"


def fit_and_cache(n_estimators: int = 600, seed: int = 0, force: bool = False) -> Path:
    """Fit the full-control model and save the booster as LightGBM text.

    Text format, not joblib: it round-trips the `pandas_categorical` line that the
    whole unit_code mapping depends on, and it does not pin us to the exact
    scikit-learn and LightGBM versions a pickle would.
    """
    dest = cache_dir() / MODEL_TXT
    if dest.exists() and not force:
        print(f"  cached: {dest}", flush=True)
        return dest

    from ..nowcast import effects

    model, df = effects.fit_full_control_model(n_estimators=n_estimators, seed=seed)
    dest.parent.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(dest))

    cats = model.booster_.pandas_categorical
    n_units = df["unit_id"].n_unique()
    expected = [list(range(1, n_units + 1))]
    if cats != expected:
        raise RuntimeError(
            f"pandas_categorical is {str(cats)[:120]}..., expected 1..{n_units}. "
            "The unit_code map is not what the serve path assumes."
        )

    meta = {
        "variant": "full_control_all_splits",
        "n_estimators": n_estimators,
        "num_trees": model.booster_.num_trees(),
        "seed": seed,
        "n_units": n_units,
        "n_features": model.booster_.num_feature(),
        "control_col": "clean_control_strict",
    }
    (dest.parent / MODEL_META).write_text(json.dumps(meta, indent=2) + "\n")
    print(f"  saved: {dest}  ({meta['num_trees']} trees, {n_units} units)", flush=True)
    return dest


def load_cached():
    """Return (booster, categories). Raises if the cache is missing."""
    import lightgbm as lgb

    path = cache_dir() / MODEL_TXT
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run fit_and_cache() first")
    booster = lgb.Booster(model_file=str(path))
    return booster, booster.pandas_categorical[0]


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="refit even if cached")
    fit_and_cache(force=ap.parse_args().force)
