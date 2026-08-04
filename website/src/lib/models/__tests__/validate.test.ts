import { describe, expect, it } from 'vitest';
import { validate } from '../validate';
import type { InputFieldDef } from '../types';

const fields: InputFieldDef[] = [
  { key: 'date', label: 'Date', kind: 'date', minDate: '2023-01-01', maxDate: '2027-12-31' },
  { key: 'start', label: 'Start', kind: 'select', options: [
    { value: 'day', label: 'Day' }, { value: 'night', label: 'Night' }], defaultValue: 'night' },
  { key: 'attendance', label: 'Attendance', kind: 'number', min: 1000, max: 42000, defaultNumber: 38000 },
  { key: 'temp_f', label: 'Temp', kind: 'number', min: 40, max: 95, required: false, defaultNumber: null },
  { key: 'rain', label: 'Rain', kind: 'toggle', defaultBool: false },
];

describe('validate', () => {
  it('accepts a full valid payload and coerces types', () => {
    const out = validate(fields, {
      date: '2026-08-14', start: 'day', attendance: '30000', temp_f: 70, rain: true,
    });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.values.attendance).toBe(30000);
      expect(out.values.rain).toBe(true);
    }
  });

  it('applies defaults for missing optional values', () => {
    const out = validate(fields, { date: '2026-08-14', attendance: 30000 });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.values.start).toBe('night');
      expect(out.values.rain).toBe(false);
      expect(out.values.temp_f).toBeNull();
    }
  });

  it('rejects out-of-range numbers with the field key', () => {
    const out = validate(fields, { date: '2026-08-14', attendance: 500 });
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.errors[0].key).toBe('attendance');
  });

  it('rejects unknown select values', () => {
    const out = validate(fields, { date: '2026-08-14', attendance: 30000, start: 'dusk' });
    expect(out.ok).toBe(false);
  });

  it('rejects malformed and out-of-window dates', () => {
    expect(validate(fields, { date: 'nope', attendance: 30000 }).ok).toBe(false);
    expect(validate(fields, { date: '2022-06-01', attendance: 30000 }).ok).toBe(false);
  });

  it('rejects a missing required field', () => {
    const out = validate(fields, { attendance: 30000 });
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.errors[0].key).toBe('date');
  });

  it('rejects non-object payloads', () => {
    expect(validate(fields, null).ok).toBe(false);
    expect(validate(fields, 'x').ok).toBe(false);
  });
});

describe('validate: place fields', () => {
  const placeFields: InputFieldDef[] = [
    { key: 'business', label: 'Your business', kind: 'place' },
  ];

  it('accepts a valid SF place and trims the name', () => {
    const out = validate(placeFields, {
      business: { lat: 37.78, lon: -122.41, name: '  My Cafe  ' },
    });
    expect(out.ok).toBe(true);
    if (out.ok) {
      const p = out.values.business as { lat: number; lon: number; name: string | null };
      expect(p.lat).toBe(37.78);
      expect(p.name).toBe('My Cafe');
    }
  });

  it('accepts a nameless pin', () => {
    const out = validate(placeFields, { business: { lat: 37.78, lon: -122.41 } });
    expect(out.ok).toBe(true);
  });

  it('rejects a missing place', () => {
    const out = validate(placeFields, {});
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.errors[0].key).toBe('business');
  });

  it('rejects coordinates outside the SF area', () => {
    expect(validate(placeFields, { business: { lat: 40.7, lon: -74.0 } }).ok).toBe(false);
  });

  it('rejects junk coordinates', () => {
    expect(validate(placeFields, { business: { lat: 'x', lon: null } }).ok).toBe(false);
  });
});
