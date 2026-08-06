import { EndpointContractError } from '../errors';
import type {
  FocusResult,
  GameInfo,
  InputValues,
  PlaceValue,
  RippleBandResult,
  RippleCellResult,
  RippleResult,
} from '../types';
import { oracleRippleConfig } from './config';
import { CELL_BY_ID, bandFor, lookupGame, nextGamesAfter, resolveCell } from './context';

/* ============================================================
   LIVE-ENDPOINT ADAPTER for the Tier 1 GBM behind SageMaker.

   Wire format: src/eia_pipeline/serve/handler/inference.py in the team repo.
   Fixtures recorded from that handler live in __tests__/fixtures/.

   The model is a COUNTERFACTUAL forecaster, so the endpoint does two things per
   request: predicts what each block would look like with no game, then applies a
   measured difference-in-differences effect on top. What comes back is already
   composed; this file only renames and validates.

   Every assertion below exists because the corresponding mistake is silent.
   ============================================================ */

const SCHEMA = 'oracle-ripple/1';

/** The ring edges we expect back, from the site's own config. */
const EXPECTED_EDGES = [
  ...oracleRippleConfig.bands.map((b) => b.innerM),
  oracleRippleConfig.bands[oracleRippleConfig.bands.length - 1].outerM,
];

interface WireBand {
  id: string;
  label: string;
  inner_m: number;
  outer_m: number | null;
  lift_pct: number;
  extra: number;
  counterfactual: number;
  n_cells: number;
  ci95_pct: [number, number] | null;
  significant: boolean;
}

interface WireCell {
  id: string;
  lift_pct: number;
  extra: number;
  counterfactual: number;
  dist_venue_m: number;
}

interface Wire {
  schema_version: string;
  model_version: string;
  measure: { id: 'visits' | 'visitor_hours'; noun: string };
  date: string;
  window: { hours: [number, number]; label: string };
  bands_m: number[];
  game: {
    home: boolean;
    start: 'day' | 'night' | null;
    first_pitch_hour: number | null;
    attendance: number | null;
    attendance_source: 'actual' | 'typical' | null;
  };
  basis: {
    observed_panel_through: string;
    projected: boolean;
    baseline_staleness_days: number | null;
    weather_source: string;
  };
  bands: WireBand[];
  cells: WireCell[];
  focus: { cell_id: string | null };
  headline: { extra_within_2p5km: number; hero_band_lift_pct: number };
}

export function buildRequest(values: InputValues): { contentType: string; body: string } {
  const place = values.business as PlaceValue;
  const date = String(values.date ?? '');
  const { cell } = resolveCell(place);

  const body = {
    schema: SCHEMA,
    date,
    lat: place.lat,
    lon: place.lon,
    /* A geometry canary. The endpoint echoes back which cell it resolved; a null
       or different echo means the site's cells.json and the model's cell_dim have
       drifted, which is PLUG-IN-ENDPOINT.md step 5 failing. Far better as an
       explicit mismatch than as a blank map nobody can explain. */
    focus_cell_id: cell?.id ?? null,
    /* Negotiated, not assumed. Both ring sets return five bands with five
       numbers, so a mismatch would relabel every bar on the page and no test
       could catch it downstream. parseResponse asserts the echo. */
    bands_m: EXPECTED_EDGES,
    include_cells: true,
  };
  /* NOTE what is NOT sent. `business.name` is a licensed Advan-derived business
     name, useless as a model feature, and would be captured by the endpoint's
     data capture. And attendance is not sent either: the endpoint has the better
     prior (previous-season day/night median, MAE 3,257 on the 49 played 2026
     games) and it is the value the model actually conditions on, so it owns it
     and the UI renders whatever comes back. */
  return { contentType: 'application/json', body: JSON.stringify(body) };
}

function bad(msg: string): never {
  throw new EndpointContractError(msg);
}

/* One decimal, the same as the simulator. The endpoint reports three, and the
   UI prints liftPct straight into the page, so without this a ring reads
   "+13.935%" against a confidence interval nearly ten points wide. */
const round1 = (n: number) => Math.round(n * 10) / 10;

export function parseResponse(body: string, values: InputValues): RippleResult {
  let p: Wire;
  if (!body || !body.trim()) bad('empty response body');
  try {
    p = JSON.parse(body) as Wire;
  } catch (e) {
    bad(`response is not JSON: ${(e as Error).message}`);
  }

  if (p.schema_version !== SCHEMA) {
    bad(`schema_version is ${JSON.stringify(p.schema_version)}, expected ${SCHEMA}`);
  }
  if (p.measure?.id !== 'visitor_hours' && p.measure?.id !== 'visits') {
    bad(`unknown measure ${JSON.stringify(p.measure)}`);
  }

  // ring edges must be the ones we asked for
  const edges = p.bands_m ?? [];
  if (
    edges.length !== EXPECTED_EDGES.length ||
    edges.some((v, i) => Math.abs(v - EXPECTED_EDGES[i]) > 1e-6)
  ) {
    bad(`bands_m ${JSON.stringify(edges)} != site rings ${JSON.stringify(EXPECTED_EDGES)}`);
  }

  const wire = p.bands ?? [];
  if (wire.length < oracleRippleConfig.bands.length) {
    bad(`got ${wire.length} bands, expected at least ${oracleRippleConfig.bands.length}`);
  }
  for (const b of oracleRippleConfig.bands) {
    if (!wire.some((w) => w.id === b.id)) bad(`response is missing band ${b.id}`);
  }

  /* Cell ids must be exactly the set the map draws. ImpactMap defaults a missing
     id to zero lift, so a drifted grid renders a flat map that looks like a
     no-game day rather than an error. */
  const cells = p.cells ?? [];
  const got = new Set(cells.map((c) => c.id));
  const unknown = [...got].filter((id) => !CELL_BY_ID.has(id));
  const missing = [...CELL_BY_ID.keys()].filter((id) => !got.has(id));
  if (unknown.length || missing.length) {
    bad(
      `cell id mismatch: ${unknown.length} unknown (${unknown.slice(0, 5).join(', ')}), ` +
        `${missing.length} missing (${missing.slice(0, 5).join(', ')}); ` +
        're-run website/scripts/build-cells.py against the model grid'
    );
  }

  const place = values.business as PlaceValue;
  const date = String(values.date ?? '');
  const scheduled = lookupGame(date);
  if (p.game.home !== scheduled.home) {
    bad(
      `endpoint says home=${p.game.home} for ${date} but the bundled schedule says ` +
        `${scheduled.home}; regenerate website/scripts/build-schedule.py`
    );
  }

  const byId = new Map(cells.map((c) => [c.id, c]));
  const outBands: RippleBandResult[] = oracleRippleConfig.bands.map((b) => {
    const w = wire.find((x) => x.id === b.id)!;
    return { id: b.id, label: b.label, liftPct: round1(w.lift_pct), extra: Math.round(w.extra) };
  });
  const outCells: RippleCellResult[] = cells.map((c) => ({
    id: c.id,
    liftPct: round1(c.lift_pct),
    extra: Math.round(c.extra),
  }));

  /* focus is resolved HERE, not taken from the response: the 400m snap radius and
     the "outside the modeled area" state are site policy. The endpoint's echo is
     only used as the drift canary. */
  const { cell, snapped } = resolveCell(place);
  /* A disagreement here is LOGGED, not fatal, and that is deliberate. Real
     geometry drift is already caught completely by the cell-id set check above,
     which compares all 452 ids. What this can also catch is a pin sitting exactly
     on a cell boundary, where Python's floor(lat/dlat) and JavaScript's
     Math.floor(lat/dlat) can land on either side of the edge. Failing the request
     for that would 502 a real user over a rounding difference that changes
     nothing, so the hard gate stays on the unambiguous check and this one just
     tells us it happened. */
  if (cell && p.focus?.cell_id && p.focus.cell_id !== cell.id) {
    console.warn(
      `[oracle-ripple] focus echo differs: endpoint ${p.focus.cell_id}, ` +
        `site ${cell.id} (boundary pin, or geometry drift the id-set check missed)`
    );
  }
  const fc = cell ? byId.get(cell.id) : undefined;
  const focus: FocusResult = cell
    ? {
        cellId: cell.id,
        distVenueM: cell.dist_venue_m,
        bandLabel: bandFor(cell.dist_venue_m)?.label ?? 'Beyond 5km',
        liftPct: round1(fc?.lift_pct ?? 0),
        extra: Math.round(fc?.extra ?? 0),
        outside: false,
        snapped,
      }
    : {
        cellId: null,
        distVenueM: null,
        bandLabel: null,
        liftPct: 0,
        extra: 0,
        outside: true,
        snapped: false,
      };

  /* The endpoint owns attendance, start and first pitch, because those are what
     the model conditioned on. Opponent is display-only and comes from the
     bundled schedule, which is the same public MLB source. */
  const game: GameInfo = p.game.home
    ? {
        home: true,
        opponent: scheduled.opponent,
        start: p.game.start ?? scheduled.start,
        firstPitchHour: p.game.first_pitch_hour ?? scheduled.firstPitchHour,
        attendance: p.game.attendance ?? undefined,
        attendanceSource: p.game.attendance_source ?? undefined,
      }
    : { home: false };

  return {
    kind: 'ripple',
    measure: p.measure,
    bands: outBands,
    cells: outCells,
    headline: {
      extraWithin2p5km: Math.round(p.headline.extra_within_2p5km),
      coreBandLiftPct: round1(p.headline.hero_band_lift_pct),
      windowLabel: p.game.home
        ? `evening (${p.window.label}) on a game day vs a matched non-game evening`
        : 'no Giants home game on this date',
    },
    focus,
    game,
    nextGames: p.game.home ? undefined : nextGamesAfter(date, 3),
    modelVersion: p.model_version,
    projected: p.basis?.projected,
    observedThrough: p.basis?.observed_panel_through,
  };
}
