import type { ModelConfig } from '../types';
import { oracleRippleConfig } from './config';

/* The Tier 2 arm: the spatiotemporal graph network (visitor-origin flow edges)
   behind its OWN SageMaker endpoint, speaking the IDENTICAL oracle-ripple/1
   wire schema. It is a second registry entry rather than a second model
   directory: adapter.ts, simulate.ts and context.ts only consume `bands`, and
   this config inherits that array BY REFERENCE, so the whole oracle-ripple
   module is shared unchanged.

   THE INHERITANCE IS BY REFERENCE ON PURPOSE, and tests assert identity (toBe,
   not toEqual) for three fields:
   - bands: one canonical ring set. Two ring sets would mean the adapter's
     EXPECTED_EDGES check no longer describes both endpoints.
   - rampMaxPct: one choropleth ceiling. The fixed-ramp argument on the base
     config ("two dates stay visually comparable") applies verbatim across
     models; two arms on different ramps would make toggling a lie.
   - fields: one input contract, validated once by the compare route.

   Framing discipline (docs/PIPELINE.md): Tier 1 is the benchmark and the graph
   did NOT improve accuracy; the two tiers are not yet scored on a common basis,
   so no MAE appears on the site. The toggle shows where two differently
   structured models DISAGREE, which is the honest reason it exists. */
export const oracleRippleStgnnConfig: ModelConfig = {
  ...oracleRippleConfig,
  id: 'oracle-ripple-stgnn',
  name: 'Game-day lift at your business (graph model)',
  /* Two keys, same as the base arm: this stays inert until
     SAGEMAKER_ENDPOINT_ORACLE_STGNN is set in Vercel. Unlike the base arm the
     compare route NEVER serves this one simulated: the simulator is shared, so
     a simulated STGNN would show numbers identical to the GBM's while claiming
     to be a different model. Not live means not shown. */
  status: 'live',
  endpointEnvVar: 'SAGEMAKER_ENDPOINT_ORACLE_STGNN',
  shortLabel: 'Tier 2 · Graph network',
  blurb:
    'Adds a spatial graph (visitor-origin flow) so related blocks inform ' +
    'each other. Same inputs, same rings; the counterfactual and the ' +
    'measured effect layer are its own.',
  tier: 2,
};
