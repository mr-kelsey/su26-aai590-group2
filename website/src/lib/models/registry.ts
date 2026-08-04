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
    parseResponse(body: string): RippleResult;
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
