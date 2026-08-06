import type { APIRoute } from 'astro';
import {
  classifyError,
  isLive,
  json,
  runModel,
  simulatedEnvelope,
} from '../../../lib/models/invoke';
import { MODELS } from '../../../lib/models/registry';
import { validate } from '../../../lib/models/validate';

// The only on-demand routes in the project; every page stays static.
export const prerender = false;

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

  try {
    if (!isLive(handle)) {
      return json(simulatedEnvelope(handle, outcome.values));
    }
    return json(await runModel(handle, outcome.values, 25_000));
  } catch (err: unknown) {
    const { status, error } = classifyError(err, handle.config.id);
    return json({ error }, status);
  }
};
