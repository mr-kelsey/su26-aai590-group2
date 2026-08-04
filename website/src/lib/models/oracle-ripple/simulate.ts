import cellsJson from '../../../data/cells.json';
import scheduleJson from '../../../data/giants-schedule.json';
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

/* Deterministic simulated preview. Every constant below is sourced from the
   team's published gold tables (su26-aai590-Group2 pipeline/build_gold.py
   outputs, 327 Giants home games 2022-2025) or labeled heuristic. The live
   endpoint replaces this whole module's OUTPUT via adapter.ts; the UI cannot
   tell the difference except through meta.source. */

export const SIM_VERSION = 'sim-2026-08-04.3';

/* Distance-decay anchors: gold distance_decay, measure=visits, slice=all.
   (ring midpoint meters, mean lift percent on game days vs matched controls) */
const DECAY_ANCHORS: ReadonlyArray<readonly [number, number]> = [
  [125, 410.7],
  [375, 26.71],
  [750, 12.12],
  [1750, 2.57],
  [3750, 0.36],
  [5000, 0],
];

/* Per-ring totals: gold event_study_ring (visits, slice=all), VERBATIM at the
   project's canonical ring edges (327 games; mean game-day daily-visit lift
   and percent lift vs matched non-game days per ring). */
const BAND_BASE: ReadonlyArray<{ id: string; extra: number; liftPct: number }> = [
  { id: 'b1', extra: 18001, liftPct: 410.7 },
  { id: 'b2', extra: 4146, liftPct: 26.71 },
  { id: 'b3', extra: 7817, liftPct: 12.12 },
  { id: 'b4', extra: 37186, liftPct: 2.57 },
  { id: 'b5', extra: 3762, liftPct: 0.36 },
];

/* Attendance scaling: core-ring lift pct by attendance tercile
   (event_study_ring slices att_t1/t2/t3 vs all: 337.34 / 404.63 / 490.13
   against 410.7). Anchor attendances are the tercile midpoints of the same
   regular-season games (MLB bronze). Piecewise linear, clamped. */
const ATT_ANCHORS: ReadonlyArray<readonly [number, number]> = [
  [25729, 0.821],
  [33112, 0.985],
  [39544, 1.193],
];

/* Day/night: core-ring pct 460.18 (day) / 375.91 (night) vs 410.7 (all). */
const START_MULT: Record<string, number> = { day: 1.12, night: 0.915 };

/* Pins that miss every modeled cell snap to the nearest cell centroid within
   this range; beyond it the spot is outside the modeled area. */
const SNAP_M = 400;

interface Cell {
  id: string; gi: number; gj: number; lat: number; lon: number;
  n_poi: number; food_share: number; dist_venue_m: number;
}
const CELLS: Cell[] = (cellsJson as { cells: Cell[] }).cells;
const CELL_BY_GRID = new Map(CELLS.map((c) => [`${c.gi}_${c.gj}`, c]));
const { dlat: DLAT, dlon: DLON } = cellsJson.meta;

interface ScheduleGame {
  d: string; dn: string; h: number; opp: string; att: number | null; gt: string;
}
const GAMES: ScheduleGame[] = (scheduleJson as { games: ScheduleGame[] }).games;
const GAME_BY_DATE = new Map(GAMES.map((g) => [g.d, g]));
const MEDIAN_ATT = scheduleJson.meta.medianAttendance as { day: number; night: number };

function attMult(attendance: number): number {
  const a = ATT_ANCHORS;
  if (attendance <= a[0][0]) return a[0][1];
  if (attendance >= a[a.length - 1][0]) return a[a.length - 1][1];
  for (let i = 1; i < a.length; i++) {
    if (attendance <= a[i][0]) {
      const t = (attendance - a[i - 1][0]) / (a[i][0] - a[i - 1][0]);
      return a[i - 1][1] + t * (a[i][1] - a[i - 1][1]);
    }
  }
  return 1;
}

/** Base decay curve: log-linear interpolation of lift pct over distance. */
export function decayPct(distM: number): number {
  const a = DECAY_ANCHORS;
  if (distM <= a[0][0]) return a[0][1];
  if (distM >= a[a.length - 1][0]) return 0;
  for (let i = 1; i < a.length; i++) {
    if (distM <= a[i][0]) {
      const [d0, p0] = a[i - 1];
      const [d1, p1] = a[i];
      const t = (Math.log(distM) - Math.log(d0)) / (Math.log(d1) - Math.log(d0));
      return Math.exp(Math.log(p0 + 1) + t * (Math.log(p1 + 1) - Math.log(p0 + 1))) - 1;
    }
  }
  return 0;
}

/** Equirectangular meters between two points; fine over a 10km city. */
function distM(latA: number, lonA: number, latB: number, lonB: number): number {
  const dy = (latA - latB) * 111320;
  const dx = (lonA - lonB) * 111320 * Math.cos((latB * Math.PI) / 180);
  return Math.sqrt(dx * dx + dy * dy);
}

/** The modeled cell holding the pin, or the nearest one within SNAP_M. */
function resolveCell(place: PlaceValue): { cell: Cell | null; snapped: boolean } {
  const gi = Math.floor(place.lat / DLAT);
  const gj = Math.floor(place.lon / DLON);
  const exact = CELL_BY_GRID.get(`${gi}_${gj}`);
  if (exact) return { cell: exact, snapped: false };
  let best: Cell | null = null;
  let bestD = Infinity;
  for (const c of CELLS) {
    const d = distM(place.lat, place.lon, c.lat, c.lon);
    if (d < bestD) {
      bestD = d;
      best = c;
    }
  }
  return bestD <= SNAP_M ? { cell: best, snapped: true } : { cell: null, snapped: false };
}

export function simulate(values: InputValues): RippleResult {
  const place = values.business as PlaceValue;
  const date = String(values.date ?? '');

  const g = GAME_BY_DATE.get(date);
  const game: GameInfo = g
    ? {
        home: true,
        opponent: g.opp,
        start: g.dn === 'day' ? 'day' : 'night',
        firstPitchHour: g.h,
        attendance: g.att ?? MEDIAN_ATT[g.dn === 'day' ? 'day' : 'night'],
        attendanceSource: g.att ? 'actual' : 'typical',
      }
    : { home: false };

  const scale = game.home
    ? attMult(game.attendance ?? MEDIAN_ATT.night) * (START_MULT[game.start ?? 'night'] ?? 1)
    : 0;

  const bands: RippleBandResult[] = oracleRippleConfig.bands.map((b) => {
    const base = BAND_BASE.find((x) => x.id === b.id);
    if (!base) throw new Error(`no base constants for band ${b.id}`);
    return {
      id: b.id,
      label: b.label,
      liftPct: round1(base.liftPct * scale),
      extra: Math.round(base.extra * scale),
    };
  });

  /* Cells: each gets the smooth decay curve's pct at its distance; a band's
     absolute extra is split across its cells by POI count weighted by that
     same curve, so dense cells near the park carry more of the total. */
  const bandFor = (d: number) =>
    oracleRippleConfig.bands.find((b) => d >= b.innerM && d < b.outerM);
  const weights = new Map<string, number>();
  const bandWeightSum = new Map<string, number>();
  for (const c of CELLS) {
    const b = bandFor(c.dist_venue_m);
    if (!b) continue;
    const w = c.n_poi * (decayPct(c.dist_venue_m) + 0.01);
    weights.set(c.id, w);
    bandWeightSum.set(b.id, (bandWeightSum.get(b.id) ?? 0) + w);
  }
  const bandExtra = new Map(bands.map((b) => [b.id, b.extra]));
  const cells: RippleCellResult[] = CELLS.map((c) => {
    const b = bandFor(c.dist_venue_m);
    const w = weights.get(c.id) ?? 0;
    const sum = b ? bandWeightSum.get(b.id) ?? 0 : 0;
    return {
      id: c.id,
      liftPct: round1(decayPct(c.dist_venue_m) * scale),
      extra: b && sum > 0 ? Math.round(((bandExtra.get(b.id) ?? 0) * w) / sum) : 0,
    };
  });
  const cellById = new Map(cells.map((c) => [c.id, c]));

  const { cell, snapped } = resolveCell(place);
  const focus: FocusResult = cell
    ? {
        cellId: cell.id,
        distVenueM: cell.dist_venue_m,
        bandLabel: bandFor(cell.dist_venue_m)?.label ?? 'Beyond 5km',
        liftPct: cellById.get(cell.id)?.liftPct ?? 0,
        extra: cellById.get(cell.id)?.extra ?? 0,
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

  const nextGames = game.home
    ? undefined
    : GAMES.filter((x) => x.d > date)
        .slice(0, 3)
        .map((x) => ({
          date: x.d,
          opponent: x.opp,
          start: (x.dn === 'day' ? 'day' : 'night') as 'day' | 'night',
        }));

  return {
    kind: 'ripple',
    measure: { id: 'visits', noun: 'visits' },
    bands,
    cells,
    headline: {
      extraWithin2p5km: bands.slice(0, 4).reduce((s, b) => s + b.extra, 0),
      coreBandLiftPct: bands[0].liftPct,
      windowLabel: game.home
        ? 'game day vs a typical non-game day'
        : 'no Giants home game on this date',
    },
    focus,
    game,
    nextGames,
  };
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}
