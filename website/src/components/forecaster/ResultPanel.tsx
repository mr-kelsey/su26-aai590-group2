import type { PredictEnvelope } from '../../lib/models/types';
import { measureCopy } from './measureCopy';

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
  const copy = measureCopy(result.measure);
  /* Beyond about 2km the measured effect's interval straddles 1%, so rendering
     "+0.4%" as a hero implies precision the estimate does not have. */
  const tooSmall = game.home && !focus.outside && focus.liftPct < 1;
  /* Derived defensively: prefer the explicit flag, fall back to comparing the
     date against the observed window, and claim nothing if neither is present. */
  const projected =
    meta.projected ??
    (meta.observedThrough ? date > meta.observedThrough : false);

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
              {tooSmall ? (
                <>
                  <p className="mt-2 text-3xl font-light tracking-tight text-fg">
                    No measurable change
                  </p>
                  <p className="mt-2 text-sm text-muted">
                    At {focus.bandLabel} from Oracle Park the game-day effect is
                    too small to separate from a normal evening.
                  </p>
                </>
              ) : (
                <>
                  <p className="mt-2 text-5xl font-light tracking-tight text-fg">
                    +{focus.liftPct}%
                  </p>
                  <p className="mt-2 text-sm text-muted">
                    {copy.blockPhrase(intFmt.format(Math.round(focus.extra)))} ·{' '}
                    {focus.bandLabel} from Oracle Park
                  </p>
                </>
              )}
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
                +{compactFmt.format(result.headline.extraWithin2p5km)} {copy.tileUnit}
              </p>
            </div>
            <div className="px-6 py-4 text-center">
              <p className="text-xs font-medium text-faint">
                {/* The 0-250m ring is a SINGLE 250m cell holding 27 businesses, so
                    the live endpoint reports its hero over 0-500m (five cells)
                    instead. Label whichever it actually sent. */}
                Core ring ({meta.source === 'live' ? '0-500m' : result.bands[0]?.label})
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
                    +{b.liftPct}% · {intFmt.format(Math.round(b.extra))}
                  </span>
                </li>
              );
            })}
            <li className="text-right text-xs text-faint">
              lift and extra {copy.tileUnit} by distance from Oracle Park
            </li>
          </ul>
        </>
      ) : null}

      {copy.gloss ? (
        <p className="border-t border-mist px-6 py-3 text-xs leading-relaxed text-faint">
          {copy.gloss}
        </p>
      ) : null}

      {meta.source === 'simulated' ? (
        <p className="border-t-2 border-red/60 bg-bg px-6 py-3 text-center text-xs leading-relaxed text-muted">
          Simulated preview: computed from the team's measured game effects, not
          the live model. The live endpoint replaces these numbers when it ships.
        </p>
      ) : projected ? (
        /* Slate, not red. Every upcoming game a user actually cares about is
           projected, so an alarm-coloured banner on all of them would be noise.
           The framing is the accurate one: the EFFECT is measured from games
           already played; what is projected is the ordinary-evening baseline it
           multiplies. */
        <p className="border-t border-mist bg-bg px-6 py-3 text-xs leading-relaxed text-muted">
          <span className="font-medium text-fg">Projected date.</span> This game is
          past the end of our measured data
          {meta.observedThrough ? ` (${fmtDate(meta.observedThrough)})` : ''}, so
          your block's normal-evening baseline is projected from the season's
          pattern rather than observed. The game-day effect applied on top of it is
          measured from games already played.
        </p>
      ) : (
        <p className="border-t border-mist bg-bg px-6 py-3 text-center text-xs text-faint">
          Live model{meta.version && meta.version !== 'live' ? ` · ${meta.version}` : ''}
        </p>
      )}
    </div>
  );
}
