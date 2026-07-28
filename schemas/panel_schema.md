# Target Panel Schema (data contract)

Every ingester lands into a common long-format panel so the modeling layer has one
shape to consume. Two related tables: the **event-day panel** (the modeling grain)
and the **crosswalk** (the join glue). Land as Parquet; partition by `year` (and
`county_fips` where large).

## Table: `event_day_panel`
The modeling grain is **event × date × geography**, long over signals.

| column           | type    | notes |
|------------------|---------|-------|
| event_id         | string  | stable key; FK to event catalog |
| venue_id         | string  | FK to `venue_crosswalk` |
| date             | date    | UTC calendar date |
| county_fips      | string  | 5-digit |
| cdtfa_city       | string  | for city-level anchor join (nullable) |
| signal           | string  | e.g. `bart_exits`, `pems_flow`, `wastewater_flow_resid`, `oi_spend_restaurants` |
| value            | double  | signal value in its native unit |
| unit             | string  | e.g. `exits`, `veh_per_hr`, `mgd`, `index_2020=100`, `usd` |
| grain            | string  | native grain before harmonization (`hour`,`day`,`week`,`quarter`) |
| is_event_day     | bool    | true on the event date (and use a separate lag flag for day+1) |
| source_id        | string  | FK to `config/sources.yaml` id |
| ingested_at      | timestamp | provenance |

Notes:
- Keep signals in **native units**; conversion factors (persons/vehicle, mode share,
  gallons/person, dollars/person-hour) live in `calibrate/`, never baked into ingest.
- Presence signals resolve at hour/day; anchors at week/quarter/year. The nowcast
  layer reconciles frequencies — do not pre-aggregate anchors down or interpolate
  presence up during ingest.
- Missing signals for a venue are **absent rows**, not zeros. Coverage varies by event
  (D8/coverage note in strategy). The DFM/state-space layer handles missingness.

## Table: `venue_crosswalk`  (see `venue_crosswalk.csv`)
The connective tissue. Maps a venue to every geography each source keys on. This is the
hidden hard part — build it carefully; most join bugs will originate here.

Columns: `venue_id, venue_name, city, county_fips, cdtfa_city, lat, lon,
sewershed_plant_id, nearest_bart_station, pems_vds_ids, event_types, notes`.

## Event catalog (upstream, not defined here)
Events come from the no-auth sports backbone + Ticketmaster/RunSignUp/Wikidata etc.
Minimum fields the panel needs: `event_id, venue_id, date, event_type,
announced_attendance (nullable), visitor_share (nullable)`.
`announced_attendance` is the calibration ground-truth; `visitor_share` enters velocity.
