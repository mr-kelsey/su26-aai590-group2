# The live capstone endpoints

**DONE 2026-08-06, TWO MODELS.** The site calls both tiers behind SageMaker. This
file is the record of how they are wired, not a to-do list. History and rationale:
`docs/superpowers/specs/2026-08-04-oracle-ripple-revamp-design.md`.

## What is wired

| | Tier 1 | Tier 2 |
|---|---|---|
| Endpoint | `eia-nowcast-oracle-ripple-v1` | `eia-nowcast-oracle-ripple-stgnn-v1` |
| Model | LightGBM counterfactual | STGNN (flow edges) counterfactual, precomputed cf grid |
| Effect layer | canonical-ring DiD on GBM residuals | its OWN DiD on STGNN residuals |
| Env var | `SAGEMAKER_ENDPOINT_ORACLE` | `SAGEMAKER_ENDPOINT_ORACLE_STGNN` |
| Registry group | `eia-nowcast-oracle-ripple` | `eia-nowcast-oracle-ripple-stgnn` |

Both: us-east-2, `ml.m5.large`, approval-gated, handler
`src/eia_pipeline/serve/handler/inference.py` (one handler, branched on the
manifest's `cf_source`), measure **visitor-hours**, hours 16-23 only, identical
`oracle-ripple/1` wire schema distinguished only by `model_version`. The Tier 2
counterfactual grid is exactly as live as the booster (both are deterministic
functions of baked artifacts; the forward pass ran at build time), and its
serve tensors are proven bit-identical to the training tensors on the whole
training overlap. Tier 2 rejects 2023-01-02 (no convolution context).
`/api/predict/compare` fans out to every live arm in one request; the Tier 2
arm is live-or-omitted, never simulated.

The model is a COUNTERFACTUAL: it predicts what a block would look like with no
game, and a separate measured effect is applied on top. The endpoint returns the
composed result, so the adapter only renames and validates.

## The six original steps, and how each was resolved

1. **Inputs.** Unchanged: business (place) + date. Day/night and attendance are
   not user inputs.
2. **Wire format.** `adapter.ts` implements `buildRequest`/`parseResponse`.
   `parseResponse(body, values)` takes the inputs too, because `focus` depends on
   the user's pin plus the site's 400m snap policy, and `game`/`nextGames` come
   from the bundled schedule that also drives the date field's bounds.
3. **Credentials.** IAM user `venue-economics-invoke` carries an inline policy
   allowing exactly `sagemaker:InvokeEndpoint` on the one endpoint ARN. Set
   `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and
   `SAGEMAKER_ENDPOINT_ORACLE` in Vercel (Preview first, then Production).
4. **Flip.** `status: 'live'` in `config.ts`. That is only ONE of two keys: the
   route also needs `SAGEMAKER_ENDPOINT_ORACLE` set, so merging does not cut
   over. Vercel binds env vars at BUILD time, so removing the var (or setting
   `ORACLE_FORCE_SIMULATED=1`) takes effect on the next deploy, not on the
   running one. The immediate lever is Vercel Instant Rollback to the prior
   production deployment, whose function still has the old environment.
5. **Geometry true-up.** DONE and now enforced in code. The model's 452 cell ids
   match `src/data/cells.json` exactly (distances agree within 0.5m), and
   `parseResponse` throws `EndpointContractError` on any id-set mismatch, because
   ImpactMap defaults an unknown id to zero lift and would otherwise render a
   flat map that looks like a no-game day.
6. **Simulator fate.** KEPT, config-gated, no automatic runtime fallback. The
   simulator speaks visits at a 410%-anchored core band and the live model speaks
   visitor-hours at roughly 50%, so a silent fallback would change both the unit
   and the magnitude under an unchanged layout. A 503 is more honest;
   `ORACLE_FORCE_SIMULATED=1` is the deliberate manual override.

## Error taxonomy

| Status | Means |
|---|---|
| 429 | endpoint throttling |
| 502 | `EndpointContractError`: the payload violates the agreed contract |
| 503 | endpoint down, not InService, or not found |
| 504 | the invoke exceeded 25s (the function ceiling is 30s) |
| 500 | anything else |

502 vs 503 matters operationally: 503 means wait, 502 means the model and the
site disagree about the wire format and someone has to look.

## Contract assertions

`parseResponse` throws on: wrong `schema_version`; `bands_m` not deep-equal to the
site's ring edges (both sets return five bands with five numbers, so a mismatch
would silently relabel every bar); a missing band; any cell-id set mismatch; and
the endpoint disagreeing with the bundled schedule about whether a date is a home
game. A focus-cell echo mismatch is LOGGED, not fatal, because a pin exactly on a
cell boundary can floor differently in Python and JavaScript and the id-set check
already covers real drift.

## Fixtures

`src/lib/models/__tests__/fixtures/endpoint-*.json` are REAL responses recorded
from the packaged handlers, not hand-written. Re-record them after any change to
a handler or its artifacts:

```bash
cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2 && uv run python -m eia_pipeline.serve.smoke --write-fixtures website/src/lib/models/__tests__/fixtures
```

```bash
cd /Users/Steve3/Projects/personal/capstone/su26-aai590-group2 && uv run python -m eia_pipeline.serve.smoke --model oracle-ripple-stgnn --write-fixtures website/src/lib/models/__tests__/fixtures
```

## Verifying without a deployed endpoint

The AWS SDK honours `AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME`, so the whole live path
including the real SDK can be exercised against a local stub that serves the
fixtures. That is how this was verified end to end before deployment.

Inputs the site sends (revision 2 persona: a business owner):

```ts
{
  business: { lat: number, lon: number, name?: string | null },  // searched POI or dropped pin
  date: 'YYYY-MM-DD',                                            // within the bundled schedule window
}
```

Day/night and attendance are NOT user inputs anymore; the server derives them
from `src/data/giants-schedule.json` (regenerate with `scripts/build-schedule.py`).
If the live endpoint wants them as features, derive them in `buildRequest()` the
same way `simulate.ts` does (schedule lookup, day/night median attendance for
future games).

Response envelope the UI consumes (both modes):

```ts
{
  model: 'oracle-ripple',
  inputs: { ...validated, normalized },
  result: {
    kind: 'ripple',
    measure: { id: 'visits' | 'visitor_hours', noun: string },
    bands: [{ id, label, liftPct, extra }],
    cells: [{ id, liftPct, extra }],   // id = c{gi}_{gj}, joins to cells.json
    headline: { extraWithin2p5km, coreBandLiftPct, windowLabel },
    focus: {                            // the user's block
      cellId, distVenueM, bandLabel, liftPct, extra,
      outside: boolean,                 // pin beyond every modeled cell
      snapped: boolean,                 // nearest-cell fallback used
    },
    game: { home, opponent?, start?, firstPitchHour?, attendance?, attendanceSource? },
    nextGames?: [{ date, opponent, start }],  // filled when game.home is false
    dollars?: { total, label },        // reserved for the calibration
  },
  meta: { source: 'simulated' | 'live', version: string },
}
```

Related route: `GET /api/places?q=` serves the business typeahead from a
server-side Advan-derived index (`src/data/pois.json`, regenerate with
`scripts/build-poi-index.py`); it is independent of the model endpoint.
