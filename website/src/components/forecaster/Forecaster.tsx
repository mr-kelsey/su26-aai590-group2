import { useState } from 'react';
import { oracleRippleConfig } from '../../lib/models/oracle-ripple/config';
import type { InputValues, PlaceValue, PredictEnvelope } from '../../lib/models/types';
import EventForm from './EventForm';
import ImpactMap from './ImpactMap';
import ResultPanel from './ResultPanel';

/* Single island owning all forecaster state. The place is shared between the
   form (search) and the map (pin drop); the result drives both the panel and
   the map choropleth. */

type Status = 'idle' | 'loading' | 'result' | 'error';

export default function Forecaster() {
  const [place, setPlace] = useState<PlaceValue | null>(null);
  const [date, setDate] = useState('');
  const [status, setStatus] = useState<Status>('idle');
  const [envelope, setEnvelope] = useState<PredictEnvelope | null>(null);
  const [error, setError] = useState('');

  async function run(values: InputValues) {
    setStatus('loading');
    setError('');
    try {
      const res = await fetch(`/api/predict/${oracleRippleConfig.id}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(values),
        // longer than the function's own 30s ceiling, so a real server-side
        // timeout still reaches the user as a message rather than a spinner
        signal: AbortSignal.timeout(35_000),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setStatus('error');
        setError(
          (body && typeof body.error === 'string' && body.error) ||
            'Something went wrong. Please try again.'
        );
        return;
      }
      setEnvelope(body as PredictEnvelope);
      setStatus('result');
    } catch {
      setStatus('error');
      setError('Network error. Could not reach the model. Try again.');
    }
  }

  function submit() {
    if (!place || !date) return;
    void run({ business: place, date });
  }

  /** "Next home game" chips re-run the forecast on that date directly. */
  function jumpToDate(d: string) {
    setDate(d);
    if (place) void run({ business: place, date: d });
  }

  function pickFromMap(lat: number, lon: number) {
    setPlace({ lat: round6(lat), lon: round6(lon), name: null });
  }

  return (
    <div className="grid w-full gap-6 lg:grid-cols-[420px_1fr] lg:items-start">
      <div className="grid gap-6">
        <EventForm
          config={oracleRippleConfig}
          place={place}
          date={date}
          loading={status === 'loading'}
          onPlaceChange={setPlace}
          onDateChange={setDate}
          onSubmit={submit}
        />
        {status === 'error' && error ? (
          <div
            role="alert"
            className="flex items-start gap-3 rounded-2xl border border-red/25 bg-red-pink/20 px-4 py-3 text-left text-sm text-red-dark"
          >
            <span aria-hidden="true" className="mt-0.5 select-none font-semibold">
              !
            </span>
            <span>{error}</span>
          </div>
        ) : null}
        <div aria-live="polite">
          {status === 'result' && envelope ? (
            <ResultPanel envelope={envelope} onJumpToDate={jumpToDate} />
          ) : null}
        </div>
      </div>
      <ImpactMap
        bands={oracleRippleConfig.bands}
        result={status === 'result' ? envelope?.result ?? null : null}
        place={place}
        focusCellId={
          status === 'result' ? envelope?.result.focus.cellId ?? null : null
        }
        onPickPlace={pickFromMap}
      />
    </div>
  );
}

function round6(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}
