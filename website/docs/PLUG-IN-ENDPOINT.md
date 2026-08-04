# Plugging in the live capstone endpoint

When the team publishes the model endpoint contract (owner: Luke), do these in
order. Until then the site serves the simulated preview and that is fine.

1. **Inputs.** Paste the real user-input list into
   `src/lib/models/oracle-ripple/config.ts` (`fields`). The form renders
   whatever is declared; kinds available: date, number, select, toggle.
2. **Wire format.** Implement `buildRequest()` and `parseResponse()` in
   `src/lib/models/oracle-ripple/adapter.ts` against the real request and
   response schema. `parseResponse` must return a `RippleResult` (see
   `src/lib/models/types.ts`); set `measure` to what the model actually emits
   (expected: visitor_hours).
3. **Credentials.** Create a long-lived IAM user in the serving AWS account
   scoped to `sagemaker:InvokeEndpoint` on the new endpoint ARN (same pattern
   as the retired 540 setup). Set `AWS_REGION`, `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`, and `SAGEMAKER_ENDPOINT_ORACLE` in the Vercel
   project and local `.env`.
4. **Flip.** Set `status: 'live'` in `config.ts`. Add adapter unit tests
   beside the existing suites in `src/lib/models/__tests__/`.
5. **Geometry true-up.** Ask Luke for `data/bronze_sf/cell_dim.parquet` and
   diff its unit_ids against `src/data/cells.json`; regenerate with
   `scripts/build-cells.py` if POI filters drifted. Ids are grid-derived on
   both sides, so mismatches should be a few edge cells at most.
6. **Simulator fate.** Keep `simulate.ts` as the badge-labeled fallback while
   the endpoint is down (the route falls back only by config today; a runtime
   fallback would be a deliberate change), or delete it once the team prefers
   hard failures. Either way the UI badge keys off `meta.source`.

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
