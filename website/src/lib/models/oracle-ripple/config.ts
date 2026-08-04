import scheduleJson from '../../../data/giants-schedule.json';
import type { ModelConfig } from '../types';

/* The volatile part of the contract. When the real input list arrives from
   the team (owner: Luke), edit THIS array; the form renders whatever is here.
   See docs/PLUG-IN-ENDPOINT.md for the full plug-in checklist.

   Persona (revision 2): a business owner asking "how much lift should my spot
   expect on a game day?". Inputs are their business location and the date;
   day/night and attendance are derived server-side from the bundled Giants
   home schedule. */
export const oracleRippleConfig: ModelConfig = {
  id: 'oracle-ripple',
  name: 'Game-day lift at your business',
  status: 'preview',
  endpointEnvVar: 'SAGEMAKER_ENDPOINT_ORACLE',
  fields: [
    {
      key: 'business',
      label: 'Your business',
      kind: 'place',
      help: 'Search by name or address, or click the map to drop a pin.',
    },
    {
      key: 'date',
      label: 'Date',
      kind: 'date',
      // the bundled schedule's coverage; regenerate scripts/build-schedule.py
      // and these move with it
      minDate: scheduleJson.meta.minDate,
      maxDate: scheduleJson.meta.maxDate,
      help: 'Any date through the end of the current schedule.',
    },
  ],
  /* The project's canonical metric rings (RING_EDGES_M in the team pipeline's
     build_silver.py, adopted 2026-07-18): 0-250m / 250-500m / 500m-1km /
     1-2.5km / 2.5-5km. The gold effect tables are native at these edges. */
  bands: [
    { id: 'b1', label: '0-250m', innerM: 0, outerM: 250 },
    { id: 'b2', label: '250-500m', innerM: 250, outerM: 500 },
    { id: 'b3', label: '500m-1km', innerM: 500, outerM: 1000 },
    { id: 'b4', label: '1-2.5km', innerM: 1000, outerM: 2500 },
    { id: 'b5', label: '2.5-5km', innerM: 2500, outerM: 5000 },
  ],
};
