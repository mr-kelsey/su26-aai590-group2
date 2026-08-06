import type { InputValues, ModelConfig, RippleResult } from './types';
import * as oracleAdapter from './oracle-ripple/adapter';
import { oracleRippleConfig } from './oracle-ripple/config';
import { simulate as oracleSimulate, SIM_VERSION } from './oracle-ripple/simulate';

export interface ModelHandle {
  config: ModelConfig;
  simVersion: string;
  simulate(values: InputValues): RippleResult;
  adapter: {
    buildRequest(values: InputValues): { contentType: string; body: string };
    /* `values` is passed because the response cannot carry everything the UI
       needs. `focus` depends on the user's pin and the site's own 400m snap
       policy, and `game`/`nextGames` come from the bundled schedule that also
       drives the date field's bounds. Having the endpoint own those would put
       site UX policy in a container on a different deploy cadence, and would let
       the form's allowed dates drift from the model's schedule. */
    parseResponse(body: string, values: InputValues): RippleResult;
  };
}

export const MODELS: Record<string, ModelHandle> = {
  [oracleRippleConfig.id]: {
    config: oracleRippleConfig,
    simVersion: SIM_VERSION,
    simulate: oracleSimulate,
    adapter: oracleAdapter,
  },
};
