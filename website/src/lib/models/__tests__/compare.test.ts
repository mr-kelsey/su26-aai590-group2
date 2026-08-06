import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import played from './fixtures/endpoint-played-night.json';

/* Same hoisted-mock pattern as route.test.ts: the SDK is mocked for the whole
   file, so no test here can ever place a real, billed AWS call. */
const { sendMock } = vi.hoisted(() => ({ sendMock: vi.fn() }));
vi.mock('@aws-sdk/client-sagemaker-runtime', () => ({
  SageMakerRuntimeClient: class {
    send = sendMock;
  },
  InvokeEndpointCommand: class {
    constructor(public input: unknown) {}
  },
}));

import { oracleRippleConfig } from '../oracle-ripple/config';
import { oracleRippleStgnnConfig } from '../oracle-ripple/config-stgnn';
import { POST } from '../../../pages/api/predict/compare';

const GBM_EP = 'eia-nowcast-oracle-ripple-v1';
const STGNN_EP = 'eia-nowcast-oracle-ripple-stgnn-v1';

/* A synthetic second-model payload derived from the recorded GBM fixture:
   same 452 cells, same six ring edges, same schema, DIFFERENT values and
   version. Placeholder until the real endpoint fixture is recorded with
   `serve.smoke --model oracle-ripple-stgnn --write-fixtures`; the derivation
   genuinely perturbs every lift so no comparison test passes vacuously. */
const stgnnWire = {
  ...played,
  model_version: 'stgnn-flow-synthetic',
  bands: played.bands.map((b) => ({
    ...b,
    lift_pct: b.lift_pct * 0.8,
    extra: b.extra * 0.9,
  })),
  cells: played.cells.map((c) => ({
    ...c,
    lift_pct: c.lift_pct * 0.8,
    extra: c.extra * 0.9,
  })),
  headline: {
    ...played.headline,
    hero_band_lift_pct: played.headline.hero_band_lift_pct * 0.8,
  },
};

beforeEach(() => {
  vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE', '');
  vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE_STGNN', '');
  sendMock.mockReset();
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

function call(body: unknown) {
  const request = new Request('http://local/api/predict/compare', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return POST({ request } as never);
}

const live = { business: { lat: 37.7801, lon: -122.3894 }, date: '2025-08-15' };
const enc = (o: unknown) => ({ Body: new TextEncoder().encode(JSON.stringify(o)) });

function goLiveBoth() {
  vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE', GBM_EP);
  vi.stubEnv('SAGEMAKER_ENDPOINT_ORACLE_STGNN', STGNN_EP);
}

/** Route by endpoint name, the shape every happy-path test wants. */
function routeByEndpoint() {
  sendMock.mockImplementation((cmd: { input: { EndpointName: string } }) =>
    Promise.resolve(enc(cmd.input.EndpointName === STGNN_EP ? stgnnWire : played))
  );
}

describe('config inheritance invariants', () => {
  it('the STGNN arm shares bands, ramp and fields BY REFERENCE', () => {
    // toBe, not toEqual: one ring set, one choropleth ceiling, one input
    // contract. Copies that merely look equal would let them drift.
    expect(oracleRippleStgnnConfig.bands).toBe(oracleRippleConfig.bands);
    expect(oracleRippleStgnnConfig.rampMaxPct).toBe(oracleRippleConfig.rampMaxPct);
    expect(oracleRippleStgnnConfig.fields).toBe(oracleRippleConfig.fields);
  });

  it('the arms differ where they must', () => {
    expect(oracleRippleStgnnConfig.id).not.toBe(oracleRippleConfig.id);
    expect(oracleRippleStgnnConfig.endpointEnvVar).not.toBe(
      oracleRippleConfig.endpointEnvVar
    );
  });
});

describe('POST /api/predict/compare', () => {
  it('400s invalid input with field errors, same shape as [model]', async () => {
    const res = await call({ date: '2025-08-15' });
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.errors[0].key).toBe('business');
  });

  it('secondary not configured: one-arm response with NO stgnn key at all', async () => {
    // The regression test for "the site is unchanged before the endpoint
    // ships". Primary not live either, so it serves the badged simulator.
    const res = await call(live);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.order).toEqual(['oracle-ripple']);
    expect(Object.keys(body.arms)).toEqual(['oracle-ripple']);
    expect(body.arms['oracle-ripple'].meta.source).toBe('simulated');
    expect(sendMock).not.toHaveBeenCalled();
  });

  it('both live and healthy: two arms, genuinely different values', async () => {
    goLiveBoth();
    routeByEndpoint();
    const res = await call(live);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.order).toEqual(['oracle-ripple', 'oracle-ripple-stgnn']);
    const a = body.arms['oracle-ripple'];
    const b = body.arms['oracle-ripple-stgnn'];
    expect(a.ok && b.ok).toBe(true);
    expect(a.meta.source).toBe('live');
    expect(b.meta.source).toBe('live');
    expect(b.meta.version).toBe('stgnn-flow-synthetic');
    // the two arms must not be the same numbers under two labels
    expect(Math.abs(a.result.focus.liftPct - b.result.focus.liftPct)).toBeGreaterThan(0.5);
    expect(sendMock).toHaveBeenCalledTimes(2);
    const eps = sendMock.mock.calls.map(
      (c: [{ input: { EndpointName: string } }]) => c[0].input.EndpointName
    );
    expect(eps).toContain(GBM_EP);
    expect(eps).toContain(STGNN_EP);
  });

  it('secondary rejects: 200, primary intact with all 452 cells', async () => {
    goLiveBoth();
    sendMock.mockImplementation((cmd: { input: { EndpointName: string } }) =>
      cmd.input.EndpointName === STGNN_EP
        ? Promise.reject(
            Object.assign(new Error('Endpoint is not InService'), {
              name: 'ValidationError',
            })
          )
        : Promise.resolve(enc(played))
    );
    const res = await call(live);
    expect(res.status).toBe(200);
    const body = await res.json();
    const a = body.arms['oracle-ripple'];
    const b = body.arms['oracle-ripple-stgnn'];
    expect(a.ok).toBe(true);
    expect(a.result.cells).toHaveLength(452);
    expect(b).toEqual({
      ok: false,
      model: 'oracle-ripple-stgnn',
      status: 503,
      error: 'The prediction service is temporarily unavailable.',
    });
  });

  it('primary rejects: 200, the secondary still renders', async () => {
    goLiveBoth();
    sendMock.mockImplementation((cmd: { input: { EndpointName: string } }) =>
      cmd.input.EndpointName === GBM_EP
        ? Promise.reject(
            Object.assign(new Error('boom'), { name: 'ThrottlingException' })
          )
        : Promise.resolve(enc(stgnnWire))
    );
    const res = await call(live);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.arms['oracle-ripple'].ok).toBe(false);
    expect(body.arms['oracle-ripple'].status).toBe(429);
    expect(body.arms['oracle-ripple-stgnn'].ok).toBe(true);
  });

  it('both reject: the PRIMARY status and message, nothing internal leaked', async () => {
    goLiveBoth();
    sendMock.mockImplementation((cmd: { input: { EndpointName: string } }) =>
      Promise.reject(
        cmd.input.EndpointName === GBM_EP
          ? Object.assign(new Error('cell id mismatch: 5 unknown'), {
              name: 'EndpointContractError',
            })
          : Object.assign(new Error('boom'), { name: 'Whatever' })
      )
    );
    const res = await call(live);
    expect(res.status).toBe(502);
    const text = JSON.stringify(await res.json());
    expect(text).not.toContain('cell id mismatch');
  });

  it('a garbage secondary payload does not take the page down', async () => {
    goLiveBoth();
    sendMock.mockImplementation((cmd: { input: { EndpointName: string } }) =>
      Promise.resolve(
        enc(cmd.input.EndpointName === STGNN_EP ? { ...played, bands_m: [0, 1] } : played)
      )
    );
    const res = await call(live);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.arms['oracle-ripple'].ok).toBe(true);
    expect(body.arms['oracle-ripple-stgnn']).toMatchObject({ ok: false, status: 502 });
  });

  it('invokes both endpoints in PARALLEL, not sequentially', async () => {
    // The test that catches an accidental `await` in a loop: with a cold
    // container that bug silently turns a ~60ms request into two cold starts
    // in series and blows the function ceiling.
    goLiveBoth();
    const resolvers: Array<(v: unknown) => void> = [];
    sendMock.mockImplementation(
      () => new Promise((resolve) => resolvers.push(resolve))
    );
    const pending = call(live);
    await vi.waitFor(() => expect(sendMock).toHaveBeenCalledTimes(2));
    expect(resolvers).toHaveLength(2); // both in flight before either resolved
    resolvers.forEach((r) => r(enc(played)));
    const res = await pending;
    expect(res.status).toBe(200);
  });

  it('a hanging secondary is aborted at its own 10s budget, primary unharmed', async () => {
    vi.useFakeTimers();
    goLiveBoth();
    sendMock.mockImplementation(
      (
        cmd: { input: { EndpointName: string } },
        opts?: { abortSignal?: AbortSignal }
      ) => {
        if (cmd.input.EndpointName === GBM_EP) return Promise.resolve(enc(played));
        return new Promise((_, reject) => {
          opts?.abortSignal?.addEventListener('abort', () =>
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
          );
        });
      }
    );
    const pending = call(live);
    await vi.advanceTimersByTimeAsync(10_000);
    const res = await pending;
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.arms['oracle-ripple'].ok).toBe(true);
    expect(body.arms['oracle-ripple-stgnn']).toMatchObject({ ok: false, status: 504 });
  });
});
