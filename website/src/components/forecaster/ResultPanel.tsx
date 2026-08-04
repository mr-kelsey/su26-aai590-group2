import type { PredictEnvelope } from '../../lib/models/types';

const intFmt = new Intl.NumberFormat('en-US');
const compactFmt = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

const dateFmt = new Intl.DateTimeFormat('en-US', {
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
});
const fmtDate = (d: string) => dateFmt.format(new Date(`${d}T00:00:00Z`));

interface Props {
  envelope: PredictEnvelope;
  onJumpToDate(date: string): void;
}

export default function ResultPanel({ envelope, onJumpToDate }: Props) {
  const { result, inputs, meta } = envelope;
  const { focus, game } = result;
  const business = inputs.business as { name?: string | null };
  const spotLabel = business?.name || 'your spot';
  const date = String(inputs.date ?? '');

  return (
    <div className="overflow-hidden rounded-3xl border border-mist bg-surface text-left shadow-[0_4px_35px_rgba(68,83,94,0.15)]">
      {game.home ? (
        <div className="border-b border-mist px-6 py-6 text-center">
          <p className="text-xs font-semibold tracking-[0.3px] text-red">
            Expected lift at {spotLabel}
          </p>
          {focus.outside ? (
            <p className="mx-auto mt-3 max-w-sm text-sm leading-relaxed text-muted">
              This spot sits outside the modeled area (the model covers blocks
              with ten or more tracked businesses). Move the pin toward the
              city and try again.
            </p>
          ) : (
            <>
              <p className="mt-2 text-5xl font-light tracking-tight text-fg">
                +{focus.liftPct}%
              </p>
              <p className="mt-2 text-sm text-muted">
                about {intFmt.format(focus.extra)} extra visits to your block ·{' '}
                {focus.bandLabel} from Oracle Park
              </p>
              {focus.snapped ? (
                <p className="mt-1 text-xs text-faint">
                  Nearest modeled block used for this estimate.
                </p>
              ) : null}
            </>
          )}
          <p className="mt-3 text-sm text-muted">
            {fmtDate(date)}: Giants vs {game.opponent},{' '}
            {game.start === 'day' ? 'day game' : 'night game'}
            {game.firstPitchHour != null
              ? ` (${game.firstPitchHour > 12 ? game.firstPitchHour - 12 : game.firstPitchHour}${game.firstPitchHour >= 12 ? 'pm' : 'am'} first pitch)`
              : ''}
            {game.attendance
              ? ` · ${intFmt.format(game.attendance)} ${game.attendanceSource === 'typical' ? 'expected (typical)' : 'attended'}`
              : ''}
          </p>
        </div>
      ) : (
        <div className="border-b border-mist px-6 py-6 text-center">
          <p className="text-xs font-semibold tracking-[0.3px] text-red">
            No Giants home game on {fmtDate(date)}
          </p>
          <p className="mt-2 text-3xl font-light tracking-tight text-fg">
            Expect a normal day
          </p>
          <p className="mx-auto mt-2 max-w-sm text-sm leading-relaxed text-muted">
            The ballpark ripple only appears on home dates. Try one of the next
            home games:
          </p>
          {result.nextGames?.length ? (
            <div className="mt-3 flex flex-wrap justify-center gap-2">
              {result.nextGames.map((g) => (
                <button
                  key={g.date}
                  type="button"
                  onClick={() => onJumpToDate(g.date)}
                  className="rounded-full border border-mist bg-bg px-4 py-1.5 text-xs font-medium text-fg transition-colors hover:border-red/50 hover:text-red"
                >
                  {fmtDate(g.date)} vs {g.opponent}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      )}

      {game.home ? (
        <>
          <div className="grid grid-cols-2 divide-x divide-mist border-b border-mist">
            <div className="px-6 py-4 text-center">
              <p className="text-xs font-medium text-faint">Citywide within 2.5km</p>
              <p className="mt-1 text-lg font-medium tabular-nums text-fg">
                +{compactFmt.format(result.headline.extraWithin2p5km)} visits
              </p>
            </div>
            <div className="px-6 py-4 text-center">
              <p className="text-xs font-medium text-faint">
                Core ring ({result.bands[0]?.label})
              </p>
              <p className="mt-1 text-lg font-medium tabular-nums text-fg">
                +{result.headline.coreBandLiftPct}%
              </p>
            </div>
          </div>

          <ul className="grid gap-3 px-6 py-5">
            {result.bands.map((b) => {
              const maxPct = Math.max(...result.bands.map((x) => x.liftPct), 1);
              return (
                <li
                  key={b.id}
                  className={`grid grid-cols-[6.5rem_1fr_auto] items-center gap-3 text-sm ${b.label === focus.bandLabel ? 'font-medium' : ''}`}
                >
                  <span className="text-muted">
                    {b.label}
                    {b.label === focus.bandLabel ? ' ←' : ''}
                  </span>
                  <span className="h-2 overflow-hidden rounded-full bg-mist">
                    <span
                      className="block h-full rounded-full bg-red"
                      style={{
                        width: `${Math.max((b.liftPct / maxPct) * 100, b.liftPct > 0 ? 2 : 0)}%`,
                      }}
                    />
                  </span>
                  <span className="tabular-nums text-fg">
                    +{b.liftPct}% · {intFmt.format(b.extra)}
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      ) : null}

      {meta.source === 'simulated' ? (
        <p className="border-t-2 border-red/60 bg-bg px-6 py-3 text-center text-xs leading-relaxed text-muted">
          Simulated preview: computed from the team's measured game effects, not
          the live model. The live endpoint replaces these numbers when it ships.
        </p>
      ) : null}
    </div>
  );
}
