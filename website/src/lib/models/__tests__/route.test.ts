import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import played from './fixtures/endpoint-played-night.json';

/* The SDK is mocked for the WHOLE file, so no test here can ever place a real,
   billed AWS call even if SAGEMAKER_ENDPOINT_ORACLE leaks in from the
   environment. vi.mock hoists above the import below, which is what makes
   mocking a module-scope client work at all. */
// vi.hoisted, not a plain const: vi.mock is lifted above the module body, so a
// plain `const sendMock` is still in its temporal dead zone when the mocked
// class is constructed at import time.
const { sendMock } = vi.hoisted(() => ({ sendMock: vi.fn() }));
vi.mock('@aws-sdk/client-sagemaker-runtime', () => ({
  SageMakerRuntimeClient: class {
    send = sendMock;
  },
  InvokeEndpointCommand: class {
    constructor(public input: unknown) {}
  },
}));

import { POST } from '../../../pages/api/predict/[model]';

/* The route reads process.env per request (not import.meta.env, and not a module
   constant), which is what makes stubbing work at all. Vitest has no config file
   here so a local .env does NOT leak in, but an inherited
   SAGEMAKER_ENDPOINT_ORACLE would send these unit tests down the live path and
   place a real, billed AWS call. Pin it explicitly rather than relying on the
   ambient environment. */
beforeEach(() => {
  vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE', '');
  // Same guard for the STGNN arm: pin it before the second endpoint even
  // exists, so an inherited var can never send a test down a billed path.
  vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE_STGNN', '');
  sendMock.mockReset();
});
afterEach(() => {
  vi.unstubAllEnvs();
});

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

  it('falls back to the simulator when the endpoint env var is unset', async () => {
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

describe('live path (SDK mocked)', () => {
  const live = { business: { lat: 37.7801, lon: -122.3894 }, date: '2025-08-15' };

  function goLive() {
    vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE', 'eia-nowcast-oracle-ripple-v1');
  }
  const ok = () => ({ Body: new TextEncoder().encode(JSON.stringify(played)) });

  it('invokes the endpoint and returns a live envelope', async () => {
    goLive();
    sendMock.mockResolvedValue(ok());
    const res = await call('oracle-ripple', live);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.meta.source).toBe('live');
    expect(body.result.measure.id).toBe('visitor_hours');
    expect(sendMock).toHaveBeenCalledTimes(1);
    const sent = JSON.parse(sendMock.mock.calls[0][0].input.Body);
    expect(sent.date).toBe('2025-08-15');
    expect(sendMock.mock.calls[0][0].input.EndpointName).toBe(
      'eia-nowcast-oracle-ripple-v1'
    );
  });

  it('meta.version is the model version, NOT the endpoint name', async () => {
    goLive();
    sendMock.mockResolvedValue(ok());
    const body = await (await call('oracle-ripple', live)).json();
    expect(body.meta.version).not.toBe('eia-nowcast-oracle-ripple-v1');
    expect(body.meta.version).toMatch(/^gbm-/);
  });

  it('ORACLE_FORCE_SIMULATED=1 rolls back without a deploy', async () => {
    goLive();
    vi.stubEnv('ORACLE_FORCE_SIMULATED', '1');
    const body = await (await call('oracle-ripple', live)).json();
    expect(body.meta.source).toBe('simulated');
    expect(sendMock).not.toHaveBeenCalled();
  });

  it.each([
    ['ThrottlingException', 'ThrottlingException', '', 429],
    ['contract violation', 'EndpointContractError', 'cell id mismatch', 502],
    ['abort', 'AbortError', '', 504],
    ['not in service', 'ValidationError', 'Endpoint is not InService', 503],
    // misconfigured deploy creds must be diagnosable, not a bare 500
    ['auth failure', 'AccessDeniedException', 'not authorized to invoke', 503],
    // SageMaker 424: the container's own BadRequest (e.g. out-of-window date)
    ['container raised', 'ModelError', 'date 2027-01-01 outside serve_window', 502],
    ['anything else', 'Whatever', 'boom', 500],
  ])('maps %s to %i', async (_n, name, message, status) => {
    goLive();
    sendMock.mockRejectedValue(Object.assign(new Error(message), { name }));
    const res = await call('oracle-ripple', live);
    expect(res.status).toBe(status);
    // never leak internals to the client
    expect(JSON.stringify(await res.json())).not.toContain('cell id mismatch');
  });

  it('a garbage payload becomes a 502, not a blank map', async () => {
    goLive();
    sendMock.mockResolvedValue({
      Body: new TextEncoder().encode(JSON.stringify({ ...played, bands_m: [0, 1] })),
    });
    expect((await call('oracle-ripple', live)).status).toBe(502);
  });
});
