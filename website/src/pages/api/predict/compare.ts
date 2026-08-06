import type { APIRoute } from 'astro';
import {
  classifyError,
  isLive,
  json,
  runModel,
  simulatedEnvelope,
} from '../../../lib/models/invoke';
import { COMPARE_ARMS, MODELS, PRIMARY_MODEL } from '../../../lib/models/registry';
import type { ArmOutcome, CompareEnvelope } from '../../../lib/models/types';
import { validate } from '../../../lib/models/validate';

/* One request, both models, so the two arms can never describe different
   inputs. Three rules are load-bearing:

   1. Promise.allSettled, never Promise.all: a failing arm must never blank the
      sibling that succeeded.
   2. Asymmetric abort budgets, both started at t=0: the primary gets 20s, the
      secondary 10s. A cold secondary container on demo day must not cost the
      GBM answer already in hand at ~400ms. Wall clock is bounded by
      max(20, 10) = 20s, inside the function's 30s ceiling.
   3. A secondary arm is attempted ONLY when it would run live, and is omitted
      from the response entirely otherwise. That keeps this whole feature inert
      until SAGEMAKER_ENDPOINT_ORACLE_STGNN exists, and it makes rendering the
      STGNN arm from the shared simulator impossible: simulated output would be
      numerically identical to the GBM's while claiming to be a second model.
      The primary keeps today's simulated-with-banner behavior.

   Status policy: 400 bad body, 404 unknown primary, 200 whenever AT LEAST ONE
   arm succeeded (partial success is a success), and on total failure the
   PRIMARY arm's status and message so the existing client error copy and the
   429/502/503/504 taxonomy keep working unchanged. */

// NOTE: this static route shadows /api/predict/compare in [model].ts's dynamic
// segment (Astro resolves static before dynamic). Never register a model whose
// id is 'compare'.
export const prerender = false;

const PRIMARY_TIMEOUT_MS = 20_000;
const SECONDARY_TIMEOUT_MS = 10_000;

export const POST: APIRoute = async ({ request }) => {
  const primary = MODELS[PRIMARY_MODEL];
  if (!primary) return json({ error: 'Unknown model.' }, 404);

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return json({ error: 'Body must be JSON.' }, 400);
  }

  /* Validated once against the primary: config-stgnn inherits `fields` by
     reference (asserted in tests), so one validation covers every arm. */
  const outcome = validate(primary.config.fields, raw);
  if (!outcome.ok) {
    return json({ error: 'Invalid input.', errors: outcome.errors }, 400);
  }
  const values = outcome.values;

  const order = COMPARE_ARMS.filter(
    (id) => id === PRIMARY_MODEL || (MODELS[id] && isLive(MODELS[id]))
  );

  const settled = await Promise.allSettled(
    order.map((id) => {
      const handle = MODELS[id];
      if (id === PRIMARY_MODEL && !isLive(handle)) {
        return Promise.resolve(simulatedEnvelope(handle, values));
      }
      return runModel(
        handle,
        values,
        id === PRIMARY_MODEL ? PRIMARY_TIMEOUT_MS : SECONDARY_TIMEOUT_MS
      );
    })
  );

  const arms: Record<string, ArmOutcome> = {};
  order.forEach((id, i) => {
    const s = settled[i];
    if (s.status === 'fulfilled') {
      arms[id] = { ok: true, model: id, result: s.value.result, meta: s.value.meta };
    } else {
      const { status, error } = classifyError(s.reason, id);
      arms[id] = { ok: false, model: id, status, error };
    }
  });

  const anyOk = order.some((id) => arms[id]?.ok);
  if (!anyOk) {
    const p = arms[PRIMARY_MODEL];
    const status = p && !p.ok ? p.status : 500;
    const error = p && !p.ok ? p.error : 'Prediction failed.';
    return json({ error, arms }, status);
  }

  const envelope: CompareEnvelope = { order, inputs: values, arms };
  return json(envelope);
};
