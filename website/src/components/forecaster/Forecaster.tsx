import { useState } from 'react';
import { oracleRippleConfig } from '../../lib/models/oracle-ripple/config';
import { MODELS, PRIMARY_MODEL } from '../../lib/models/registry';
import type {
  CompareEnvelope,
  InputValues,
  PlaceValue,
  PredictEnvelope,
} from '../../lib/models/types';
import CompareStrip from './CompareStrip';
import EventForm from './EventForm';
import ImpactMap from './ImpactMap';
import ModelToggle from './ModelToggle';
import ResultPanel from './ResultPanel';

/* Single island owning all forecaster state. The place is shared between the
   form (search) and the map (pin drop). One request to /api/predict/compare
   returns every usable model arm, so both arms always describe the same
   inputs; the toggle switches which arm drives the panel and the map.

   The arm transition is deliberately NOT animated: an instant swap is a blink
   comparator, and the blocks that flicker are exactly the blocks where the two
   models disagree. The focus outline stays put (focus is site policy resolved
   from the pin), which keeps the flicker readable. */

type Status = 'idle' | 'loading' | 'result' | 'error';

export default function Forecaster() {
  const [place, setPlace] = useState<PlaceValue | null>(null);
  const [date, setDate] = useState('');
  const [status, setStatus] = useState<Status>('idle');
  const [cmp, setCmp] = useState<CompareEnvelope | null>(null);
  const [arm, setArm] = useState<string>(PRIMARY_MODEL);
  const [armNotice, setArmNotice] = useState<string | null>(null);
  const [error, setError] = useState('');

  const usable = cmp ? cmp.order.filter((id) => cmp.arms[id]?.ok) : [];
  const activeArm = usable.includes(arm) ? arm : (usable[0] ?? PRIMARY_MODEL);
  const envelope = envelopeFor(cmp, activeArm);

  async function run(values: InputValues) {
    setStatus('loading');
    setError('');
    try {
      const res = await fetch('/api/predict/compare', {
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
      const next = body as CompareEnvelope;
      const ok = next.order.filter((id) => next.arms[id]?.ok);
      /* If the selected arm failed this run but another answered, switch to
         the survivor and SAY SO: auto-switching is fine, silently is not. */
      if (!ok.includes(arm) && ok.length) {
        const dropped = MODELS[arm]?.config.shortLabel ?? arm;
        setArm(ok[0]);
        setArmNotice(ok.length < next.order.length
          ? `${dropped} didn't respond for this date.`
          : null);
      } else {
        const missing = next.order.filter((id) => !next.arms[id]?.ok);
        setArmNotice(
          missing.length
            ? `${missing
                .map((id) => MODELS[id]?.config.shortLabel ?? id)
                .join(', ')} didn't respond for this date.`
            : null
        );
      }
      setCmp(next);
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
        {/* The toggle is a control, not live content, so it sits OUTSIDE the
            aria-live region below. */}
        {status === 'result' && (usable.length > 1 || armNotice) ? (
          <ModelToggle
            arms={usable}
            active={activeArm}
            onChange={setArm}
            notice={armNotice}
          />
        ) : null}
        <div aria-live="polite">
          {status === 'result' && envelope ? (
            <div className="grid gap-6">
              <ResultPanel envelope={envelope} onJumpToDate={jumpToDate} />
              {cmp ? <CompareStrip cmp={cmp} /> : null}
            </div>
          ) : null}
        </div>
      </div>
      <ImpactMap
        bands={oracleRippleConfig.bands}
        /* ONE ramp across arms and dates. The fixed-ceiling argument on the
           config ("two dates stay visually comparable") applies verbatim
           across models; per-arm ramps would make the blink comparator lie. */
        rampMaxPct={oracleRippleConfig.rampMaxPct}
        result={status === 'result' ? envelope?.result ?? null : null}
        place={place}
        focusCellId={
          status === 'result' ? envelope?.result.focus.cellId ?? null : null
        }
        armLabel={
          status === 'result' && usable.length > 1
            ? MODELS[activeArm]?.config.shortLabel ?? null
            : null
        }
        onPickPlace={pickFromMap}
      />
    </div>
  );
}

/** Synthesize the per-model envelope ResultPanel has always consumed. */
function envelopeFor(
  cmp: CompareEnvelope | null,
  id: string
): PredictEnvelope | null {
  const arm = cmp?.arms[id];
  if (!cmp || !arm?.ok) return null;
  return { model: id, inputs: cmp.inputs, result: arm.result, meta: arm.meta };
}

function round6(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}
