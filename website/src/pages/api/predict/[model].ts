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
  const live = handle.config.status === 'live' && !!endpoint;

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
    const res = await sagemaker.send(
      new InvokeEndpointCommand({
        EndpointName: endpoint,
        ContentType: req.contentType,
        Body: req.body,
      })
    );
    const envelope: PredictEnvelope = {
      model: handle.config.id,
      inputs: outcome.values,
      result: handle.adapter.parseResponse(new TextDecoder().decode(res.Body)),
      meta: { source: 'live', version: endpoint! },
    };
    return json(envelope);
  } catch (err: unknown) {
    // Same taxonomy as the retired 540 route: clean statuses, no stack leaks.
    const e = err as { name?: string; message?: string };
    const name = e?.name ?? '';
    const message = e?.message ?? '';
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
