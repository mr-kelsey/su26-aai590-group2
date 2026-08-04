import type { FieldError, InputFieldDef, InputValues, PlaceValue, ValidationOutcome } from './types';

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/* Generous SF-area bounds for place pins; the simulator handles precise
   in-cell resolution itself. */
const PLACE_LAT = [37.6, 37.9] as const;
const PLACE_LON = [-122.6, -122.25] as const;

/** Server-side validation and coercion of one model's inputs.
    Unknown keys in `raw` are ignored on purpose: the config is the contract. */
export function validate(fields: InputFieldDef[], raw: unknown): ValidationOutcome {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { ok: false, errors: [{ key: '', message: 'Body must be a JSON object.' }] };
  }
  const src = raw as Record<string, unknown>;
  const values: InputValues = {};
  const errors: FieldError[] = [];

  for (const f of fields) {
    const required = f.required !== false;
    const has = src[f.key] !== undefined && src[f.key] !== null && src[f.key] !== '';

    if (!has) {
      if (f.kind === 'toggle') values[f.key] = f.defaultBool ?? false;
      else if (f.kind === 'select' && f.defaultValue !== undefined) values[f.key] = f.defaultValue;
      else if (f.kind === 'number' && f.defaultNumber !== undefined) values[f.key] = f.defaultNumber;
      else if (required) errors.push({ key: f.key, message: `${f.label} is required.` });
      else values[f.key] = null;
      continue;
    }

    const v = src[f.key];
    switch (f.kind) {
      case 'number': {
        const n = typeof v === 'number' ? v : Number(String(v));
        if (!Number.isFinite(n)) errors.push({ key: f.key, message: `${f.label} must be a number.` });
        else if (f.min !== undefined && n < f.min) errors.push({ key: f.key, message: `${f.label} must be at least ${f.min}.` });
        else if (f.max !== undefined && n > f.max) errors.push({ key: f.key, message: `${f.label} must be at most ${f.max}.` });
        else values[f.key] = n;
        break;
      }
      case 'select': {
        const s = String(v);
        if (!f.options?.some((o) => o.value === s)) errors.push({ key: f.key, message: `${f.label} has an unknown value.` });
        else values[f.key] = s;
        break;
      }
      case 'toggle': {
        values[f.key] = v === true || v === 'true';
        break;
      }
      case 'date': {
        const s = String(v);
        const t = DATE_RE.test(s) ? Date.parse(`${s}T00:00:00Z`) : NaN;
        if (Number.isNaN(t)) errors.push({ key: f.key, message: `${f.label} must be YYYY-MM-DD.` });
        else if (f.minDate && s < f.minDate) errors.push({ key: f.key, message: `${f.label} must be on or after ${f.minDate}.` });
        else if (f.maxDate && s > f.maxDate) errors.push({ key: f.key, message: `${f.label} must be on or before ${f.maxDate}.` });
        else values[f.key] = s;
        break;
      }
      case 'place': {
        const p = v as Partial<PlaceValue>;
        const lat = Number(p?.lat);
        const lon = Number(p?.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
          errors.push({ key: f.key, message: `${f.label} needs a location (search or click the map).` });
        } else if (lat < PLACE_LAT[0] || lat > PLACE_LAT[1] || lon < PLACE_LON[0] || lon > PLACE_LON[1]) {
          errors.push({ key: f.key, message: `${f.label} must be in the San Francisco area.` });
        } else {
          const name = typeof p?.name === 'string' ? p.name.trim().slice(0, 120) : null;
          values[f.key] = { lat, lon, name } satisfies PlaceValue;
        }
        break;
      }
    }
  }

  return errors.length ? { ok: false, errors } : { ok: true, values };
}
