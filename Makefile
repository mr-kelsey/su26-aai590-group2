.PHONY: setup lint test config
setup:      ## create env from pyproject
	uv sync --extra model --extra dev --extra charts
figures:    ## render every report figure to docs/reports/figures/
	uv run python -m eia_pipeline.eda.charts
profile:    ## write docs/07_data_profile.md from the modelling panel
	uv run python -m eia_pipeline.eda.profile
config:     ## print resolved settings (no secret values)
	uv run python -c "from eia_pipeline.settings import settings; print(settings.summary())"
lint:
	uv run ruff check src
test:
	uv run pytest -q
