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

// The index stores street-only addresses ("128 King St"), but users type or
// paste full mailing addresses. These pin the normalized token matching.
describe('GET /api/places address normalization', () => {
  async function names(q: string): Promise<string[]> {
    const body = await (await call(q)).json();
    return body.results.map((r: { name: string }) => r.name);
  }

  it('matches a full mailing address pasted from a map app', async () => {
    const body = await (await call('128 King St, San Francisco, CA 94107')).json();
    expect(body.results[0].address).toBe('128 King St');
    expect(body.results.map((r: { name: string }) => r.name)).toContain('Underdogs Cantina');
  });

  it('matches a spelled-out street suffix against an abbreviated address', async () => {
    expect(await names('128 King Street')).toContain('Underdogs Cantina');
  });

  it('ignores punctuation in the query', async () => {
    expect(await names('128 King St.')).toContain('Underdogs Cantina');
  });

  it('drops locality words written without commas', async () => {
    expect(await names('128 king st san francisco')).toContain('Underdogs Cantina');
  });

  it('matches tokens split across name and address', async () => {
    expect(await names('underdogs king st')).toContain('Underdogs Cantina');
  });

  it('ignores apostrophes in names', async () => {
    expect(await names('barrys bootcamp')).toContain("BARRY'S BOOTCAMP SAN FRANCISCO");
  });

  it('still searches locality words when the query is nothing but locality', async () => {
    expect((await names('san francisco')).length).toBeGreaterThan(0);
  });
});
