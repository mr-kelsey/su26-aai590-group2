import type { InputValues, RippleResult } from '../types';

/* ============================================================
   LIVE-ENDPOINT ADAPTER: the two functions the pending spec fills in.
   Wire format unknown until the team publishes the endpoint contract
   (owner: Luke). Follow docs/PLUG-IN-ENDPOINT.md when it lands.
   While config.status is 'preview' the route never calls these.
   ============================================================ */

export function buildRequest(_values: InputValues): { contentType: string; body: string } {
  throw new Error('oracle-ripple live adapter not implemented: endpoint spec pending');
}

export function parseResponse(_body: string, _values: InputValues): RippleResult {
  throw new Error('oracle-ripple live adapter not implemented: endpoint spec pending');
}
