.PHONY: setup lint test config
setup:      ## create env from pyproject
	uv sync --extra model --extra dev
config:     ## print resolved settings (no secret values)
	uv run python -c "from eia_pipeline.settings import settings; print(settings.summary())"
lint:
	uv run ruff check src
test:
	uv run pytest -q
