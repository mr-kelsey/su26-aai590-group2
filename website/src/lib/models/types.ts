/* Shared model-registry types. The adapter boundary (buildRequest /
   parseResponse) is where the pending endpoint spec plugs in. */

export type FieldKind = 'date' | 'number' | 'select' | 'toggle' | 'place';

/** A user-chosen location: a searched business or a dropped map pin. */
export interface PlaceValue {
  lat: number;
  lon: number;
  name?: string | null;
}

export interface FieldOption {
  value: string;
  label: string;
}

export interface InputFieldDef {
  key: string;
  label: string;
  kind: FieldKind;
  /** default true */
  required?: boolean;
  /** render inside the collapsed Advanced section */
  advanced?: boolean;
  help?: string;
  // number
  min?: number;
  max?: number;
  step?: number;
  defaultNumber?: number | null;
  // select
  options?: FieldOption[];
  defaultValue?: string;
  // toggle
  defaultBool?: boolean;
  // date (inclusive, YYYY-MM-DD)
  minDate?: string;
  maxDate?: string;
}

export interface BandDef {
  id: string;
  label: string;
  innerM: number;
  outerM: number;
}

export interface ModelConfig {
  id: string;
  name: string;
  status: 'preview' | 'live';
  /** env var holding the SageMaker endpoint name when live */
  endpointEnvVar: string;
  fields: InputFieldDef[];
  bands: BandDef[];
  /** choropleth ceiling in lift percent; see the note on the value itself */
  rampMaxPct: number;
}

export type InputValues = Record<string, string | number | boolean | PlaceValue | null>;

export interface RippleBandResult {
  id: string;
  label: string;
  liftPct: number;
  extra: number;
}

export interface RippleCellResult {
  id: string;
  liftPct: number;
  extra: number;
}

/** What the chosen date holds at Oracle Park. */
export interface GameInfo {
  home: boolean;
  opponent?: string;
  start?: 'day' | 'night';
  firstPitchHour?: number;
  attendance?: number;
  attendanceSource?: 'actual' | 'typical';
}

/** The lift at the user's own block (the 250m cell holding their pin). */
export interface FocusResult {
  cellId: string | null;
  distVenueM: number | null;
  bandLabel: string | null;
  liftPct: number;
  extra: number;
  /** pin fell outside every modeled cell (and none within snap range) */
  outside: boolean;
  /** pin was snapped to the nearest modeled cell centroid */
  snapped: boolean;
}

export interface RippleResult {
  kind: 'ripple';
  /** unit the numbers are in; the live model speaks visitor-hours,
      the simulator speaks daily visits */
  measure: { id: 'visits' | 'visitor_hours'; noun: string };
  bands: RippleBandResult[];
  cells: RippleCellResult[];
  headline: {
    /** sum of the four inner rings (0-2.5km); ring 5 is the near-zero edge */
    extraWithin2p5km: number;
    coreBandLiftPct: number;
    windowLabel: string;
  };
  focus: FocusResult;
  game: GameInfo;
  /** next home dates after the chosen date, filled when game.home is false */
  nextGames?: { date: string; opponent: string; start: 'day' | 'night' }[];
  dollars?: { total: number; label: string };
  /* Provenance the LIVE adapter fills in and the route lifts onto `meta`.
     They ride here because parseResponse returns a RippleResult and nothing
     else, and widening that signature to a tuple would touch every caller for
     three optional strings. The simulator leaves all three unset. */

  /** the model's own version string, so meta.version does not have to leak the
      internal AWS endpoint name into a public response */
  modelVersion?: string;
  /** this date's features are projected past the observed window */
  projected?: boolean;
  /** last date covered by observed features, e.g. '2026-05-31' */
  observedThrough?: string;
}

export interface PredictEnvelope {
  model: string;
  inputs: InputValues;
  result: RippleResult;
  meta: {
    source: 'simulated' | 'live';
    version: string;
    /** live only: this date's features are projected past the observed window.
        Distinct from game.attendanceSource === 'typical' (attendance guessed);
        both are true for every 2026 date. */
    projected?: boolean;
    /** live only: last date covered by observed features, e.g. '2026-05-31' */
    observedThrough?: string;
  };
}

export interface FieldError {
  key: string;
  message: string;
}

export type ValidationOutcome =
  | { ok: true; values: InputValues }
  | { ok: false; errors: FieldError[] };
