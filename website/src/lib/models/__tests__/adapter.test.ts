import { describe, expect, it } from 'vitest';
import cellsJson from '../../../data/cells.json';
import { EndpointContractError } from '../errors';
import { buildRequest, parseResponse } from '../oracle-ripple/adapter';
import { simulate } from '../oracle-ripple/simulate';
import type { InputValues, PlaceValue } from '../types';
import played from './fixtures/endpoint-played-night.json';
import projected from './fixtures/endpoint-projected-2026.json';
import noGame from './fixtures/endpoint-no-game.json';
import stgnnPlayed from './fixtures/endpoint-stgnn-played-night.json';

/* Fixtures are REAL responses recorded from the packaged handler
   (src/eia_pipeline/serve/handler/inference.py), not hand-written. A hand-written
   fixture only proves the adapter agrees with my idea of the wire format. */

const cells = (cellsJson as { cells: { id: string; lat: number; lon: number }[] }).cells;
const near = cells.find((c) => c.id === 'c16791_-43096')!;
const IN_CELL: PlaceValue = { lat: near.lat, lon: near.lon, name: 'Test Cafe' };
const FAR: PlaceValue = { lat: 37.62, lon: -122.5, name: null };

const vPlayed: InputValues = { business: IN_CELL, date: '2025-08-15' };
const vProjected: InputValues = { business: IN_CELL, date: '2026-08-07' };
const vNoGame: InputValues = { business: IN_CELL, date: '2026-01-15' };

const body = (o: unknown) => JSON.stringify(o);

describe('buildRequest', () => {
  it('sends the date, the pin and the negotiated ring edges', () => {
    const req = buildRequest(vPlayed);
    expect(req.contentType).toBe('application/json');
    const b = JSON.parse(req.body);
    expect(b.date).toBe('2025-08-15');
    expect(b.lat).toBeCloseTo(near.lat, 6);
    expect(b.bands_m).toEqual([0, 250, 500, 1000, 2500, 5000]);
    expect(b.focus_cell_id).toBe('c16791_-43096');
  });

  it('never sends the business name', () => {
    // licensed Advan-derived data, useless as a feature, and would land in the
    // endpoint's data capture
    expect(buildRequest(vPlayed).body).not.toContain('Test Cafe');
  });

  it('does not send attendance: the endpoint owns it', () => {
    const b = JSON.parse(buildRequest(vPlayed).body);
    expect(b.attendance).toBeUndefined();
  });
});

describe('parseResponse', () => {
  it('maps a played game onto the UI shape', () => {
    const r = parseResponse(body(played), vPlayed);
    expect(r.kind).toBe('ripple');
    expect(r.measure.id).toBe('visitor_hours');
    expect(r.bands).toHaveLength(5);
    expect(r.bands.map((b) => b.id)).toEqual(['b1', 'b2', 'b3', 'b4', 'b5']);
    expect(r.cells).toHaveLength(cells.length);
    expect(r.game.home).toBe(true);
    expect(r.game.attendanceSource).toBe('actual');
    expect(r.modelVersion).toMatch(/^gbm-/);
    expect(r.projected).toBe(false);
  });

  it('resolves focus from the INPUTS, not from the response', () => {
    const inCell = parseResponse(body(played), vPlayed);
    expect(inCell.focus.cellId).toBe('c16791_-43096');
    expect(inCell.focus.outside).toBe(false);

    const far = parseResponse(body(played), { business: FAR, date: '2025-08-15' });
    expect(far.focus.outside).toBe(true);
    expect(far.focus.cellId).toBeNull();
  });

  it('carries the projected flag and the observed window', () => {
    const r = parseResponse(body(projected), vProjected);
    expect(r.projected).toBe(true);
    expect(r.observedThrough).toBe('2026-05-31');
    expect(r.game.attendanceSource).toBe('typical');
  });

  it('returns zero lift and next games for a no-game date', () => {
    const r = parseResponse(body(noGame), vNoGame);
    expect(r.game.home).toBe(false);
    expect(r.bands.every((b) => b.liftPct === 0)).toBe(true);
    expect(r.nextGames).toHaveLength(3);
  });

  it('band extras reconcile with the sum of their cells', () => {
    const r = parseResponse(body(played), vPlayed);
    const wire = played as unknown as {
      cells: { id: string; extra: number; dist_venue_m: number }[];
      bands: { id: string; inner_m: number; outer_m: number | null; extra: number }[];
    };
    for (const b of wire.bands) {
      const hi = b.outer_m ?? Infinity;
      const sum = wire.cells
        .filter((c) => c.dist_venue_m >= b.inner_m && c.dist_venue_m < hi)
        .reduce((s, c) => s + c.extra, 0);
      expect(Math.abs(sum - b.extra)).toBeLessThanOrEqual(Math.max(1, 0.001 * Math.abs(b.extra)));
    }
    expect(r.headline.extraWithin2p5km).toBeGreaterThan(0);
  });

  it('carries per-band significance so a suppressed band stays legible', () => {
    /* The effect layer ships a band as EXACT ZERO when its bootstrap CI spans
       zero (effects_v2.py honesty rule). In the STGNN arm that fires for
       1-2.5km while 2.5-5km stays positive, and without this flag the UI
       renders "not distinguishable from zero" identically to "zero effect",
       which got reported as a prediction bug. */
    const gbm = parseResponse(body(played), vPlayed);
    expect(gbm.bands.map((b) => b.significant)).toEqual([true, true, true, true, true]);

    const stgnn = parseResponse(body(stgnnPlayed), vPlayed);
    expect(stgnn.bands.map((b) => b.significant)).toEqual([true, true, true, false, true]);
    const b4 = stgnn.bands.find((b) => b.id === 'b4')!;
    expect(b4.liftPct).toBe(0);
  });

  it('agrees with the simulator on game and nextGames', () => {
    // proves context.ts is genuinely SHARED rather than duplicated
    expect(parseResponse(body(noGame), vNoGame).nextGames).toEqual(
      simulate(vNoGame).nextGames
    );
    expect(parseResponse(body(played), vPlayed).game.opponent).toBe(
      simulate(vPlayed).game.opponent
    );
  });
});

describe('contract violations all throw EndpointContractError', () => {
  // loosely typed view of the fixture so the mutations below stay readable
  const P = played as unknown as Record<string, never> & {
    cells: Record<string, unknown>[];
    bands: unknown[];
    game: Record<string, unknown>;
  };
  const cases: [string, unknown][] = [
    ['wrong schema', { ...P, schema_version: 'nope/9' }],
    ['ring edges disagree', { ...P, bands_m: [0, 500, 1000, 2000, 4000, 8000] }],
    ['unknown cell id', { ...P, cells: [{ ...P.cells[0], id: 'c1_1' }] }],
    ['missing cells', { ...P, cells: [] }],
    ['missing a band', { ...P, bands: P.bands.slice(1) }],
    ['schedule disagrees about the game', { ...P, game: { ...P.game, home: false } }],
  ];
  for (const [name, payload] of cases) {
    it(name, () => {
      expect(() => parseResponse(body(payload), vPlayed)).toThrow(EndpointContractError);
    });
  }

  it('empty body', () => {
    expect(() => parseResponse('', vPlayed)).toThrow(EndpointContractError);
  });

  it('non-JSON body', () => {
    expect(() => parseResponse('<html>502</html>', vPlayed)).toThrow(EndpointContractError);
  });
});
