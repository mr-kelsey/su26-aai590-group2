import type { RippleResult } from '../../lib/models/types';

type MeasureId = RippleResult['measure']['id'];

/* The UI must render whichever unit the result declares. It previously hardcoded
   "visits", which was fine while only the simulator existed and becomes a lie the
   moment the live model answers in visitor-hours.

   The COPY lives here rather than in the payload on purpose. `measure` crosses
   the network from SageMaker, and endpoint output is data, not trusted UI text;
   putting display strings in it would let a model change silently rewrite the
   page. The payload declares an id, the site decides what to say about it. */

export interface MeasureCopy {
  /** e.g. "about 1,240 extra visitor-hours on your block" */
  blockPhrase(formatted: string): string;
  /** unit label for a compact stat tile */
  tileUnit: string;
  /** one-paragraph explanation, or null when the unit needs none */
  gloss: string | null;
}

const COPY: Record<MeasureId, MeasureCopy> = {
  visits: {
    blockPhrase: (n) => `about ${n} extra visits to your block`,
    tileUnit: 'visits',
    gloss: null,
  },
  visitor_hours: {
    blockPhrase: (n) => `about ${n} extra visitor-hours on your block`,
    tileUnit: 'visitor-hours',
    /* "visitor-hours", not "customers" and not a headcount. The pipeline's own
       note is blunt about it: one unit is one estimated visitor present during
       one hourly bucket, so a four-hour visit counts four times, and
       visitor-hours must never be added to visits. Dividing by four to fake a
       headcount would be exactly the unit-mixing this project calls out as its
       own lesson: that ratio is a venue-specific median, not a conversion.

       The number a business owner actually acts on is the percentage, and a
       ratio is unit-free, so the headline means the same thing either way. */
    gloss:
      'A visitor-hour is one person present for one hour, so it is not a ' +
      'headcount: someone who stays three hours counts three times. Measured ' +
      'over the 4pm to 11pm window, against a matched non-game evening.',
  },
};

export function measureCopy(m: RippleResult['measure']): MeasureCopy {
  return (
    COPY[m.id] ?? {
      // an unrecognized future unit still renders sanely off the declared noun
      blockPhrase: (n) => `about ${n} extra ${m.noun}`,
      tileUnit: m.noun,
      gloss: null,
    }
  );
}
