import { describe, expect, it } from 'vitest';
import { GET } from '../../../pages/api/places';

function call(q: string) {
  return GET({ url: new URL(`http://local/api/places?q=${encodeURIComponent(q)}`) } as never);
}

describe('GET /api/places', () => {
  it('400s queries under 2 characters', async () => {
    const res = await call('a');
    expect(res.status).toBe(400);
  });

  it('returns at most 8 ranked matches with the expected fields', async () => {
    const res = await call('coffee');
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.results.length).toBeGreaterThan(0);
    expect(body.results.length).toBeLessThanOrEqual(8);
    const r = body.results[0];
    expect(typeof r.name).toBe('string');
    expect(typeof r.address).toBe('string');
    expect(Number.isFinite(r.lat)).toBe(true);
    expect(Number.isFinite(r.lon)).toBe(true);
  });

  it('matches street addresses too', async () => {
    const res = await call('market st');
    const body = await res.json();
    expect(body.results.length).toBeGreaterThan(0);
  });

  it('returns an empty list for gibberish', async () => {
    const res = await call('zzqqxxyy');
    const body = await res.json();
    expect(body.results).toEqual([]);
  });
});
