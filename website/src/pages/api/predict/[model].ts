import type { APIRoute } from 'astro';
import {
  InvokeEndpointCommand,
  SageMakerRuntimeClient,
} from '@aws-sdk/client-sagemaker-runtime';
import { MODELS } from '../../../lib/models/registry';
import { validate } from '../../../lib/models/validate';
import type { PredictEnvelope } from '../../../lib/models/types';

// The only on-demand routes in the project; every page stays static.
export const prerender = false;

function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const sagemaker = new SageMakerRuntimeClient({
  region: process.env.AWS_REGION ?? 'us-east-2',
});

export const POST: APIRoute = async ({ params, request }) => {
  const handle = MODELS[params.model ?? ''];
  if (!handle) return json({ error: 'Unknown model.' }, 404);

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return json({ error: 'Body must be JSON.' }, 400);
  }

  const outcome = validate(handle.config.fields, raw);
  if (!outcome.ok) {
    return json({ error: 'Invalid input.', errors: outcome.errors }, 400);
  }

  const endpoint = process.env[handle.config.endpointEnvVar];
  // Two keys plus a manual override. The env var is the staged-rollout lever
  // (set it in Preview, verify, then Production), and ORACLE_FORCE_SIMULATED is
  // a 30-second rollback to the badged simulator with no deploy. It can never
  // fire by accident, and the red "Simulated preview" banner tells the truth
  // when it does.
  const live =
    handle.config.status === 'live' &&
    !!endpoint &&
    process.env.ORACLE_FORCE_SIMULATED !== '1';

  try {
    if (!live) {
      const envelope: PredictEnvelope = {
        model: handle.config.id,
        inputs: outcome.values,
        result: handle.simulate(outcome.values),
        meta: { source: 'simulated', version: handle.simVersion },
      };
      return json(envelope);
    }

    const req = handle.adapter.buildRequest(outcome.values);
    // A cold SageMaker container can take tens of seconds. Without this the
    // Vercel function hits its own plan limit first and returns a bodiless 504,
    // which the client turns into a generic "something went wrong".
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 25_000);
    let res;
    try {
      res = await sagemaker.send(
        new InvokeEndpointCommand({
          EndpointName: endpoint,
          ContentType: req.contentType,
          Body: req.body,
        }),
        { abortSignal: ac.signal }
      );
    } finally {
      clearTimeout(timer);
    }
    const text = res.Body ? new TextDecoder().decode(res.Body) : '';
    const result = handle.adapter.parseResponse(text, outcome.values);
    const envelope: PredictEnvelope = {
      model: handle.config.id,
      inputs: outcome.values,
      result,
      // NOT the endpoint name: that is an internal AWS resource identifier and
      // this response is public.
      meta: {
        source: 'live',
        version: result.modelVersion ?? 'live',
        ...(result.projected === undefined ? {} : { projected: result.projected }),
        ...(result.observedThrough === undefined
          ? {}
          : { observedThrough: result.observedThrough }),
      },
    };
    return json(envelope);
  } catch (err: unknown) {
    // Same taxonomy as the retired 540 route: clean statuses, no stack leaks.
    // ORDER MATTERS: these are substring tests on `name`, so the most specific
    // cases have to come first.
    const e = err as { name?: string; message?: string };
    const name = e?.name ?? '';
    const message = e?.message ?? '';
    if (name === 'EndpointContractError') {
      // the detail stays server-side; the client gets a clean status. This is
      // what makes a wrong-shaped payload diagnosable instead of a blank map.
      console.error(`[predict] contract violation: ${message}`);
      return json({ error: 'The model returned an unexpected response.' }, 502);
    }
    if (name === 'AbortError' || name === 'TimeoutError') {
      return json({ error: 'The model took too long to respond. Please retry.' }, 504);
    }
    if (name.includes('ThrottlingException')) {
      return json({ error: 'The model is busy, please retry.' }, 429);
    }
    if (
      name.includes('ValidationError') ||
      /not.*InService|OutOfService|could not be found|ResourceNotFound/i.test(message)
    ) {
      return json({ error: 'The prediction service is temporarily unavailable.' }, 503);
    }
    return json({ error: 'Prediction failed.' }, 500);
  }
};
