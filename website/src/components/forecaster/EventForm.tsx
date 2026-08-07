import { useEffect, useId, useRef, useState } from 'react';
import type { InputFieldDef, ModelConfig, PlaceValue } from '../../lib/models/types';

/* Config-driven form. Renders whatever fields the model config declares;
   'place' gets the business search + pin affordance, wired to state owned by
   the parent island (the map sets the same place). */

interface Props {
  config: ModelConfig;
  place: PlaceValue | null;
  date: string;
  loading: boolean;
  onPlaceChange(place: PlaceValue | null): void;
  onDateChange(date: string): void;
  onSubmit(): void;
}

const CONTROL_CLASS =
  'w-full rounded-2xl border border-mist bg-surface px-4 py-3 text-fg outline-none ' +
  'transition-colors placeholder:text-faint focus:border-red/60 focus:ring-2 focus:ring-red/30 ' +
  'disabled:opacity-50';

export default function EventForm({
  config,
  place,
  date,
  loading,
  onPlaceChange,
  onDateChange,
  onSubmit,
}: Props) {
  const labelId = useId();
  const ready = !!place && !!date;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (ready && !loading) onSubmit();
      }}
      aria-labelledby={`${labelId}-title`}
      className="rounded-3xl border border-mist bg-surface p-6 text-left shadow-[0_4px_35px_rgba(68,83,94,0.15)] sm:p-7"
    >
      <h2 id={`${labelId}-title`} className="sr-only">
        {config.name}
      </h2>
      <div className="grid gap-5">
        {config.fields.map((f) =>
          f.kind === 'place' ? (
            <PlaceField
              key={f.key}
              def={f}
              place={place}
              disabled={loading}
              onChange={onPlaceChange}
            />
          ) : f.kind === 'date' ? (
            <DateField
              key={f.key}
              def={f}
              value={date}
              disabled={loading}
              onChange={onDateChange}
            />
          ) : null
        )}

        <button
          type="submit"
          disabled={loading || !ready}
          className="mt-1 inline-flex items-center justify-center gap-2 rounded-full bg-red px-8 py-3.5 text-[15px] font-semibold tracking-[0.3px] text-white transition-colors duration-300 hover:bg-red-dark focus:outline-none focus:ring-2 focus:ring-red/50 focus:ring-offset-2 focus:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-60"
        >
          {loading ? 'Estimating your lift...' : 'Estimate my game-day lift'}
        </button>
        {!place ? (
          <p className="text-xs text-faint">
            Pick your business above or click your spot on the map.
          </p>
        ) : null}
      </div>
    </form>
  );
}

interface SearchResult {
  name: string;
  address: string;
  lat: number;
  lon: number;
}

function PlaceField({
  def,
  place,
  disabled,
  onChange,
}: {
  def: InputFieldDef;
  place: PlaceValue | null;
  disabled: boolean;
  onChange(place: PlaceValue | null): void;
}) {
  const id = useId();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  // Debounced server-side search; aborts stale requests.
  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setResults([]);
      setOpen(false);
      return;
    }
    const ctl = new AbortController();
    const t = setTimeout(async () => {
      setSearching(true);
      try {
        const res = await fetch(`/api/places?q=${encodeURIComponent(q)}`, {
          signal: ctl.signal,
        });
        const body = await res.json().catch(() => null);
        const found: SearchResult[] = body?.results ?? [];
        setResults(found);
        setOpen(true);
      } catch {
        /* aborted or offline; keep the previous list */
      } finally {
        setSearching(false);
      }
    }, 250);
    return () => {
      ctl.abort();
      clearTimeout(t);
    };
  }, [query]);

  // Close the dropdown on outside click.
  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  function pick(r: SearchResult) {
    onChange({ lat: r.lat, lon: r.lon, name: r.name });
    setQuery('');
    setOpen(false);
  }

  return (
    <div className="grid gap-2" ref={boxRef}>
      <label htmlFor={id} className="text-sm font-medium text-muted">
        {def.label}
      </label>

      {place ? (
        <div className="flex items-center justify-between gap-3 rounded-2xl border border-mist bg-bg px-4 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-fg">
              {place.name ?? 'Dropped pin'}
            </p>
            <p className="truncate text-xs text-faint">
              {place.lat.toFixed(4)}, {place.lon.toFixed(4)}
            </p>
          </div>
          <button
            type="button"
            onClick={() => onChange(null)}
            disabled={disabled}
            className="shrink-0 rounded-full border border-mist bg-surface px-3 py-1 text-xs font-medium text-muted transition-colors hover:border-red/40 hover:text-red disabled:opacity-50"
          >
            Change
          </button>
        </div>
      ) : (
        <div className="relative">
          <input
            id={id}
            type="text"
            value={query}
            disabled={disabled}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => results.length > 0 && setOpen(true)}
            placeholder="Search name or address"
            autoComplete="off"
            role="combobox"
            aria-expanded={open}
            aria-controls={`${id}-listbox`}
            className={CONTROL_CLASS}
          />
          {searching ? (
            <span
              aria-hidden="true"
              className="absolute right-4 top-1/2 h-2 w-2 -translate-y-1/2 animate-pulse rounded-full bg-red"
            />
          ) : null}
          {open ? (
            <ul
              id={`${id}-listbox`}
              role="listbox"
              className="absolute z-20 mt-2 max-h-72 w-full overflow-auto rounded-2xl border border-mist bg-surface py-1 shadow-[0_12px_50px_rgba(52,57,65,0.10)]"
            >
              {results.length === 0 ? (
                <li className="px-4 py-3 text-sm text-faint">
                  No matches. Try just the street address, like 128 King St, or click your
                  spot on the map.
                </li>
              ) : (
                results.map((r, i) => (
                  <li key={`${r.lat},${r.lon},${i}`} role="option" aria-selected="false">
                    <button
                      type="button"
                      onClick={() => pick(r)}
                      className="block w-full px-4 py-2.5 text-left transition-colors hover:bg-bg"
                    >
                      <span className="block truncate text-sm font-medium text-fg">
                        {r.name}
                      </span>
                      <span className="block truncate text-xs text-faint">{r.address}</span>
                    </button>
                  </li>
                ))
              )}
            </ul>
          ) : null}
        </div>
      )}

      {def.help && !place ? <p className="text-xs text-faint">{def.help}</p> : null}
    </div>
  );
}

function DateField({
  def,
  value,
  disabled,
  onChange,
}: {
  def: InputFieldDef;
  value: string;
  disabled: boolean;
  onChange(v: string): void;
}) {
  const id = useId();
  return (
    <div className="grid gap-2">
      <label htmlFor={id} className="text-sm font-medium text-muted">
        {def.label}
      </label>
      <input
        id={id}
        type="date"
        value={value}
        min={def.minDate}
        max={def.maxDate}
        required={def.required !== false}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={CONTROL_CLASS}
      />
      {def.help ? <p className="text-xs text-faint">{def.help}</p> : null}
    </div>
  );
}
