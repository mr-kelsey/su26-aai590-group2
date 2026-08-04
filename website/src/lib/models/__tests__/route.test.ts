import { describe, expect, it } from 'vitest';
import { POST } from '../../../pages/api/predict/[model]';

function call(model: string, body: unknown) {
  const request = new Request(`http://local/api/predict/${model}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  // Astro's APIRoute context is wider than this; the route only reads these.
  return POST({ params: { model }, request } as never);
}

const good = {
  business: { lat: 37.7815, lon: -122.3925, name: 'Test Cafe' },
  date: '2025-07-04',
};

describe('POST /api/predict/[model]', () => {
  it('404s an unknown model', async () => {
    const res = await call('nope', good);
    expect(res.status).toBe(404);
  });

  it('400s invalid input with field errors', async () => {
    const res = await call('oracle-ripple', { date: '2025-07-04' });
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.errors[0].key).toBe('business');
  });

  it('returns a simulated envelope for valid input', async () => {
    const res = await call('oracle-ripple', good);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.model).toBe('oracle-ripple');
    expect(body.meta.source).toBe('simulated');
    expect(body.result.kind).toBe('ripple');
    expect(body.result.bands).toHaveLength(5);
    expect(body.result.focus).toBeDefined();
    expect(body.result.game).toBeDefined();
    expect(body.inputs.business.lat).toBeCloseTo(37.7815, 4);
  });

  it('400s a non-JSON body', async () => {
    const request = new Request('http://local/api/predict/oracle-ripple', {
      method: 'POST', body: 'not json',
    });
    const res = await POST({ params: { model: 'oracle-ripple' }, request } as never);
    expect(res.status).toBe(400);
  });
});
