import cellsJson from '../../../data/cells.json';
import scheduleJson from '../../../data/giants-schedule.json';
import type { BandDef, GameInfo, PlaceValue } from '../types';
import { oracleRippleConfig } from './config';

/* Everything about WHERE and WHEN, shared by the simulator and the live adapter.

   Both paths have to answer the same two questions before they can say anything
   useful: which of the 452 modeled cells is the user standing in, and what is
   happening at Oracle Park that day. Those answers are site policy, not model
   output: the 400m snap radius, the "outside the modeled area" fallback, and the
   bundled schedule that also drives the date field's min and max. If the live
   path derived them separately the map could outline one block while the number
   described another, and no test would catch it.

   WHAT IS DELIBERATELY NOT HERE: the simulator's constants. DECAY_ANCHORS,
   BAND_BASE, ATT_ANCHORS, START_MULT and attMult stay in simulate.ts, because
   they are transcribed gold-table numbers for the preview. If the adapter could
   reach them, "live" results would sooner or later inherit a simulator anchor
   and nobody would notice. */

export const SNAP_M = 400;

export interface Cell {
  id: string;
  gi: number;
  gj: number;
  lat: number;
  lon: number;
  n_poi: number;
  food_share: number;
  dist_venue_m: number;
}

export const CELLS: Cell[] = (cellsJson as { cells: Cell[] }).cells;
export const CELL_BY_GRID = new Map(CELLS.map((c) => [`${c.gi}_${c.gj}`, c]));
export const CELL_BY_ID = new Map(CELLS.map((c) => [c.id, c]));
export const { dlat: DLAT, dlon: DLON } = cellsJson.meta;

export interface ScheduleGame {
  d: string;
  dn: string;
  h: number;
  opp: string;
  att: number | null;
  gt: string;
}

export const GAMES: ScheduleGame[] = (scheduleJson as { games: ScheduleGame[] }).games;
export const GAME_BY_DATE = new Map(GAMES.map((g) => [g.d, g]));
export const MEDIAN_ATT = scheduleJson.meta.medianAttendance as {
  day: number;
  night: number;
};

/** Equirectangular meters between two points; fine over a 10km city. */
export function distM(latA: number, lonA: number, latB: number, lonB: number): number {
  const dy = (latA - latB) * 111320;
  const dx = (lonA - lonB) * 111320 * Math.cos((latB * Math.PI) / 180);
  return Math.sqrt(dx * dx + dy * dy);
}

/** The modeled cell holding the pin, or the nearest one within SNAP_M. */
export function resolveCell(place: PlaceValue): { cell: Cell | null; snapped: boolean } {
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

/** The band a distance falls in, by the config's canonical ring edges. */
export function bandFor(distVenueM: number): BandDef | undefined {
  return oracleRippleConfig.bands.find(
    (b) => distVenueM >= b.innerM && distVenueM < b.outerM
  );
}

/** What the chosen date holds at Oracle Park, from the bundled schedule. */
export function lookupGame(date: string): GameInfo {
  const g = GAME_BY_DATE.get(date);
  if (!g) return { home: false };
  const start = g.dn === 'day' ? 'day' : 'night';
  return {
    home: true,
    opponent: g.opp,
    start,
    firstPitchHour: g.h,
    attendance: g.att ?? MEDIAN_ATT[start],
    attendanceSource: g.att ? 'actual' : 'typical',
  };
}

/** The next `n` home dates strictly after `date`. */
export function nextGamesAfter(
  date: string,
  n = 3
): { date: string; opponent: string; start: 'day' | 'night' }[] {
  return GAMES.filter((x) => x.d > date)
    .slice(0, n)
    .map((x) => ({
      date: x.d,
      opponent: x.opp,
      start: (x.dn === 'day' ? 'day' : 'night') as 'day' | 'night',
    }));
}
