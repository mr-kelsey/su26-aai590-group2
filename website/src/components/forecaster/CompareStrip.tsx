import { MODELS } from '../../lib/models/registry';
import type { ArmOutcome, CompareEnvelope } from '../../lib/models/types';

const intFmt = new Intl.NumberFormat('en-US');

/* Both models' numbers side by side, with the gap in percentage points.

   Three suppression rules, each guarding a way the strip could fabricate a
   claim:
   1. Both arms must be LIVE. The arms share one simulator, so a simulated pair
      would show a delta of exactly 0.0, a fabricated claim of perfect
      agreement.
   2. Both arms must speak the SAME measure. Visits minus visitor-hours is the
      unit-mixing this project calls out as its own lesson.
   3. No strip on a no-game date: both arms are zero and "0.0 points apart" is
      noise.

   Framing is symmetric on purpose: "N points apart", never "off by N". No
   arrows, no green/red, no winner. The model_version footer is what makes the
   comparison auditable. */

interface Props {
  cmp: CompareEnvelope;
}

export default function CompareStrip({ cmp }: Props) {
  const live = cmp.order
    .map((id) => cmp.arms[id])
    .filter((a): a is Extract<ArmOutcome, { ok: true }> => !!a?.ok)
    .filter((a) => a.meta.source === 'live');
  if (live.length < 2) return null;

  const [a, b] = live;
  if (a.result.measure.id !== b.result.measure.id) return null;
  if (!a.result.game.home || !b.result.game.home) return null;
  const outside = a.result.focus.outside || b.result.focus.outside;

  const deltaFocus = Math.abs(a.result.focus.liftPct - b.result.focus.liftPct);
  const label = (o: Extract<ArmOutcome, { ok: true }>) =>
    MODELS[o.model]?.config.shortLabel ?? o.model;

  return (
    <div className="overflow-hidden rounded-3xl border border-mist bg-surface text-left shadow-[0_4px_35px_rgba(68,83,94,0.15)]">
      <p className="border-b border-mist px-6 py-3 text-xs font-semibold tracking-[0.3px] text-red">
        The two models, same date, same block
      </p>
      <div className="grid grid-cols-[1fr_auto_auto] items-center gap-x-4 gap-y-2 px-6 py-4 text-sm">
        <span className="text-xs font-medium text-faint" />
        <span className="text-xs font-medium text-faint">{label(a)}</span>
        <span className="text-xs font-medium text-faint">{label(b)}</span>

        {!outside ? (
          <>
            <span className="text-muted">At your block</span>
            <span className="tabular-nums text-fg">+{a.result.focus.liftPct}%</span>
            <span className="tabular-nums text-fg">+{b.result.focus.liftPct}%</span>
          </>
        ) : null}

        <span className="text-muted">Citywide within 2.5km</span>
        <span className="tabular-nums text-fg">
          +{intFmt.format(a.result.headline.extraWithin2p5km)}
        </span>
        <span className="tabular-nums text-fg">
          +{intFmt.format(b.result.headline.extraWithin2p5km)}
        </span>
      </div>
      {!outside ? (
        <p className="border-t border-mist px-6 py-3 text-xs text-muted">
          The two estimates at your block are{' '}
          <span className="font-medium text-fg">
            {deltaFocus.toFixed(1)} points apart
          </span>
          . Where they disagree is where the estimate leans on model structure
          rather than measurement.
        </p>
      ) : null}
      <p className="border-t border-mist bg-bg px-6 py-2 text-center text-[10px] text-faint">
        {a.meta.version} · {b.meta.version}
      </p>
    </div>
  );
}
