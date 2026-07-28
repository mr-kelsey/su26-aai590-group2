# EIA Daily Nowcast Pipeline

Estimating the **daily economic impact of social events** across California by
fusing high-frequency presence proxies against slow dollar anchors — i.e.
**nowcasting** daily spend that no source measures directly.

This repo is the data-pipeline layer of the AAI-590 capstone. It is designed to be
built out **incrementally inside Claude Code**. Start by reading `CLAUDE.md`, then
`docs/00_project_overview.md`, then run `tasks/01_first_task.md`.

## Quickstart
```bash
uv sync                      # create env from pyproject.toml (Python 3.11)
cp .env.example .env         # fill in ONLY what you need; most first-task sources need nothing
uv run python -c "from eia_pipeline.settings import settings; print(settings.summary())"
```

## What's here
| Path | Purpose |
|------|---------|
| `CLAUDE.md` | Orientation for the coding agent — read first |
| `docs/` | Strategy, modeling framework, and the decisions log (with rationale) |
| `config/sources.yaml` | Machine-readable registry of every data source + status flag |
| `schemas/` | Target panel schema and the venue↔geography crosswalk |
| `tasks/` | The scoped first task and the backlog |
| `src/eia_pipeline/` | The pipeline package (stubs to build out) |

## The core equation
```
daily_spend ≈ presence(t) × spending_velocity(segment, category, place)
```
Presence is cheap and fast. Velocity is calibrated against dollar anchors
(CDTFA taxable sales, Opportunity Insights spend, Transient Occupancy Tax).
See `docs/02_modeling_and_nowcasting.md`.

## Non-negotiables
- Config over hardcoding; secrets are pointers, never values.
- Only ingest sources listed in `config/sources.yaml`; respect every license.
- Every proxy carries an explicit, calibrated conversion factor — never assumed.
