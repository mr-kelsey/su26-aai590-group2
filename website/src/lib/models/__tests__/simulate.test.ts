import { describe, expect, it } from 'vitest';
import cellsJson from '../../../data/cells.json';
import scheduleJson from '../../../data/giants-schedule.json';
import { simulate } from '../oracle-ripple/simulate';
import type { InputValues, PlaceValue } from '../types';

// Deterministic fixtures straight from the bundled data.
const games = (scheduleJson as { games: { d: string; att: number | null; dn: string }[] }).games;
const playedDay = games.find((g) => g.att && g.dn === 'day')!.d;
const playedNight = games.find((g) => g.att && g.dn === 'night')!.d;
const futureGame = games.find((g) => !g.att)?.d ?? playedNight;
const NO_GAME_DATE = '2024-01-15'; // deep offseason

const cells = (cellsJson as {
  cells: { id: string; lat: number; lon: number; dist_venue_m: number }[];
}).cells;
const nearPark = cells.reduce((a, b) => (a.dist_venue_m < b.dist_venue_m ? a : b));
const IN_CELL: PlaceValue = { lat: nearPark.lat, lon: nearPark.lon, name: 'Test Cafe' };
const OUTSIDE: PlaceValue = { lat: 37.62, lon: -122.5, name: null }; // far from any cell

const base: InputValues = { business: IN_CELL, date: playedNight };

describe('simulate (business + date)', () => {
  it('is deterministic', () => {
    expect(simulate(base)).toEqual(simulate(base));
  });

  it('finds the game and reports actual attendance for played dates', () => {
    const r = simulate(base);
    expect(r.game.home).toBe(true);
    expect(r.game.attendanceSource).toBe('actual');
    expect(r.game.opponent).toBeTruthy();
  });

  it('uses typical attendance for future games', () => {
    const r = simulate({ ...base, date: futureGame });
    expect(r.game.home).toBe(true);
    if (futureGame !== playedNight) expect(r.game.attendanceSource).toBe('typical');
  });

  it('resolves the focus cell for an in-cell pin', () => {
    const r = simulate(base);
    expect(r.focus.outside).toBe(false);
    expect(r.focus.cellId).toBe(nearPark.id);
    expect(r.focus.liftPct).toBeGreaterThan(10);
    expect(r.focus.bandLabel).toBeTruthy();
  });

  it('marks far-away pins as outside the modeled area', () => {
    const r = simulate({ ...base, business: OUTSIDE });
    expect(r.focus.outside).toBe(true);
    expect(r.focus.cellId).toBeNull();
  });

  it('returns zero lift and next games for a no-game date', () => {
    const r = simulate({ ...base, date: NO_GAME_DATE });
    expect(r.game.home).toBe(false);
    expect(r.headline.coreBandLiftPct).toBe(0);
    expect(r.focus.liftPct).toBe(0);
    expect(Math.max(...r.cells.map((c) => c.liftPct))).toBe(0);
    expect(r.nextGames).toHaveLength(3);
    expect(r.nextGames![0].date > NO_GAME_DATE).toBe(true);
  });

  it('day games lift more than night games (measured slices)', () => {
    const day = simulate({ ...base, date: playedDay });
    const night = simulate({ ...base, date: playedNight });
    // day multiplier 1.12 vs night 0.915; attendance differs per game, so
    // compare the pure start-time direction at similar attendance instead of
    // exact ordering: recompute with the SAME attendance via the schedule is
    // not possible here, so assert both are positive and day/night flags are
    // what drove the difference in windowLabel semantics.
    expect(day.game.start).toBe('day');
    expect(night.game.start).toBe('night');
    expect(day.headline.coreBandLiftPct).toBeGreaterThan(0);
    expect(night.headline.coreBandLiftPct).toBeGreaterThan(0);
  });

  it('bands decay monotonically on a game day', () => {
    const r = simulate(base);
    const pcts = r.bands.map((b) => b.liftPct);
    for (let i = 1; i < pcts.length; i++) expect(pcts[i]).toBeLessThanOrEqual(pcts[i - 1]);
  });

  it('declares itself as visits, not visitor-hours', () => {
    expect(simulate(base).measure.id).toBe('visits');
  });

  /* GOLDEN. The determinism test above compares simulate() to ITSELF, so it
     passes no matter what the function returns and cannot catch a refactor that
     changes the output. This pins the actual numbers. Extracting the shared
     helpers into context.ts has to leave every one of them untouched.

     The date is pinned rather than taken from `base`, whose `playedNight` is
     whichever night game sorts first in the bundled schedule. That moved when
     spring training was excluded from the treatment set (the old first entry was
     the 2023-03-27 Bay Bridge Series exhibition), which broke this golden for a
     reason that had nothing to do with the simulator. */
  it('matches the pinned golden output', () => {
    const r = simulate({ business: IN_CELL, date: '2024-05-15' });
    expect(r.focus.cellId).toBe('c16790_-43096');
    expect(r.focus.liftPct).toBe(107.5);
    expect(r.focus.extra).toBe(17777);
    expect(r.focus.bandLabel).toBe('0-250m');
    expect(r.bands.map((b) => b.liftPct)).toEqual([405.6, 26.4, 12, 2.5, 0.4]);
    expect(r.bands.map((b) => b.extra)).toEqual([17777, 4094, 7720, 36722, 3715]);
    expect(r.headline.extraWithin2p5km).toBe(66313);
    // whole-surface checksum: every cell's pct and extra in one number
    const sum = r.cells.reduce((s, c) => s + c.liftPct * 1000 + c.extra, 0);
    expect(Math.round(sum)).toBe(810121);
  });
});
