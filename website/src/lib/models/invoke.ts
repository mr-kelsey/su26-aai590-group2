/* Shared server-side plumbing for the predict routes. One SageMaker client,
   one live/simulated gate, one error taxonomy, so /api/predict/[model] and any
   fan-out route classify failures identically instead of drifting apart. */
import {
  InvokeEndpointCommand,
  SageMakerRuntimeClient,
} from '@aws-sdk/client-sagemaker-runtime';
import type { ModelHandle } from './registry';
import type { InputValues, PredictEnvelope } from './types';

export function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

export const sagemaker = new SageMakerRuntimeClient({
  region: process.env.AWS_REGION ?? 'us-east-2',
});

/* Two keys plus a manual override. The env var is the staged-rollout lever
   (set it in Vercel Preview, verify, then Production; a change takes effect on
   the NEXT deploy, not on a running one), and ORACLE_FORCE_SIMULATED forces
   the badged simulator on the next deploy without touching the endpoint. For
   an immediate rollback use Vercel Instant Rollback to the prior deployment.
   Read process.env per request, not at module scope: that is what lets tests
   stub it and Vercel inject it. */
export function isLive(handle: ModelHandle): boolean {
  return (
    handle.config.status === 'live' &&
    !!process.env[handle.config.endpointEnvVar] &&
    process.env.ORACLE_FORCE_SIMULATED !== '1'
  );
}

export function simulatedEnvelope(
  handle: ModelHandle,
  values: InputValues
): PredictEnvelope {
  return {
    model: handle.config.id,
    inputs: values,
    result: handle.simulate(values),
    meta: { source: 'simulated', version: handle.simVersion },
  };
}

/** Invoke the model's live endpoint and build the public envelope. Throws on
    any failure; callers classify with classifyError(). */
export async function runModel(
  handle: ModelHandle,
  values: InputValues,
  timeoutMs: number
): Promise<PredictEnvelope> {
  const endpoint = process.env[handle.config.endpointEnvVar];
  const req = handle.adapter.buildRequest(values);
  // A cold SageMaker container can take tens of seconds. Without this the
  // Vercel function hits its own plan limit first and returns a bodiless 504,
  // which the client turns into a generic "something went wrong".
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
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
  const result = handle.adapter.parseResponse(text, values);
  return {
    model: handle.config.id,
    inputs: values,
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
}

/** Map a live-path failure to a clean public status. The detail stays
    server-side (console.error on EVERY branch, so nothing fails silently);
    the client gets a stable message with no stack or resource names.
    ORDER MATTERS: these are substring tests on `name`, so the most specific
    cases have to come first. */
export function classifyError(
  err: unknown,
  modelId: string
): { status: number; error: string } {
  const e = err as { name?: string; message?: string };
  const name = e?.name ?? '';
  const message = e?.message ?? '';
  if (name === 'EndpointContractError') {
    // what makes a wrong-shaped payload diagnosable instead of a blank map
    console.error(`[predict:${modelId}] contract violation: ${message}`);
    return { status: 502, error: 'The model returned an unexpected response.' };
  }
  if (name === 'AbortError' || name === 'TimeoutError') {
    console.error(`[predict:${modelId}] timed out`);
    return { status: 504, error: 'The model took too long to respond. Please retry.' };
  }
  if (name.includes('ThrottlingException')) {
    console.error(`[predict:${modelId}] throttled`);
    return { status: 429, error: 'The model is busy, please retry.' };
  }
  // SageMaker 424: the CONTAINER itself raised (e.g. the handler's BadRequest
  // for an out-of-window date). Without this branch it reads as a bare 500.
  if (name === 'ModelError') {
    console.error(`[predict:${modelId}] model error: ${message}`);
    return { status: 502, error: 'The model rejected this request.' };
  }
  // Credential and signing failures: misconfigured deploy, not a user error.
  if (
    name.includes('AccessDenied') ||
    name === 'UnrecognizedClientException' ||
    name === 'InvalidSignatureException'
  ) {
    console.error(`[predict:${modelId}] auth failure: ${name} ${message}`);
    return { status: 503, error: 'The prediction service is not configured correctly.' };
  }
  if (
    name.includes('ValidationError') ||
    /not.*InService|OutOfService|could not be found|ResourceNotFound/i.test(message)
  ) {
    console.error(`[predict:${modelId}] endpoint unavailable: ${name} ${message}`);
    return { status: 503, error: 'The prediction service is temporarily unavailable.' };
  }
  console.error(`[predict:${modelId}] unhandled ${name}: ${message}`);
  return { status: 500, error: 'Prediction failed.' };
}
