"""Everything that turns the Tier 1 GBM into something a website can call.

`nowcast/` fits the model and measures the effect. This package takes those two
results and makes them servable: it rebuilds the panel, extends the covariates
past the observed window so a future game date can be scored, re-estimates the
effect layer at the canonical ring edges, freezes the whole lot into a
`model.tar.gz`, and deploys it behind a SageMaker endpoint.

Nothing in `nowcast/` is modified by any of this. The training window stays
2023-01-02..2025-12-31 and every published metric still reproduces from
`data/bronze_sf/model_hour.parquet` bit for bit; the serve path builds parallel
tables and leaves the originals alone.
"""
