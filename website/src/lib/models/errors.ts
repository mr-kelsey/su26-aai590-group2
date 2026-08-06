/** The endpoint answered, but the payload violates the agreed contract.

    Deliberately distinct from "the endpoint is down" (503) and "we crashed"
    (500), because the operator response is different: a 503 means wait, a 502
    here means the model and the site disagree about the wire format and someone
    has to go look.

    This is what turns the worst failure mode in this integration into a
    diagnosable one. ImpactMap reads only `result.cells[].liftPct` and defaults a
    missing id to 0, so a response with the wrong cell ids renders a flat all-zero
    choropleth that is indistinguishable from a no-game day. Throwing here, with
    the mismatch in the message, means that shows up as a 502 in the logs instead
    of as a map somebody eventually notices looks wrong. */
export class EndpointContractError extends Error {
  readonly name = 'EndpointContractError';
}
