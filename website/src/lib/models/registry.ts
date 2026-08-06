import type { InputValues, ModelConfig, RippleResult } from './types';
import * as oracleAdapter from './oracle-ripple/adapter';
import { oracleRippleConfig } from './oracle-ripple/config';
import { oracleRippleStgnnConfig } from './oracle-ripple/config-stgnn';
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
  /* The Tier 2 arm shares the adapter, simulator and context wholesale: they
     consume only config.bands, which config-stgnn inherits by reference. The
     shared simulator is also why the compare route never runs this arm
     simulated (see the config's own comment). */
  [oracleRippleStgnnConfig.id]: {
    config: oracleRippleStgnnConfig,
    simVersion: SIM_VERSION,
    simulate: oracleSimulate,
    adapter: oracleAdapter,
  },
};

/** The benchmark arm: what /api/predict/compare always attempts, what the UI
    selects by default, and whose status/error a total failure reports. */
export const PRIMARY_MODEL = oracleRippleConfig.id;

/** Display order for the compare route and the segmented control. */
export const COMPARE_ARMS = [oracleRippleConfig.id, oracleRippleStgnnConfig.id];
