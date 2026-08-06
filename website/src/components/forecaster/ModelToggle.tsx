import { MODELS } from '../../lib/models/registry';

/* Segmented control between model arms. Renders ONLY when more than one arm is
   usable (live and answering), and only usable arms get a segment: a disabled
   or broken segment would let the user select an arm with no result, which
   would strand the map on the previous arm's choropleth under the wrong label.

   Both segments carry identical visual weight on purpose: the site must not
   imply the graph model is an upgrade. Tier 1 is the benchmark (the neutral
   chip says so) and the project's own report says the graph did not improve
   accuracy. No MAE appears here: the two tiers are not scored on a common
   basis (docs/PIPELINE.md), so a head-to-head number would overstate what the
   comparison establishes. */

interface Props {
  arms: string[];
  active: string;
  onChange(id: string): void;
  /** one line under the toggle when an arm dropped out, e.g. a 503 */
  notice?: string | null;
}

export default function ModelToggle({ arms, active, onChange, notice }: Props) {
  if (arms.length < 2 && !notice) return null;
  const activeConfig = MODELS[active]?.config;
  return (
    <div className="text-left">
      {arms.length > 1 ? (
        <div
          role="tablist"
          aria-label="Model"
          className="inline-flex rounded-full border border-mist bg-bg p-1"
        >
          {arms.map((id) => {
            const c = MODELS[id]?.config;
            const selected = id === active;
            return (
              <button
                key={id}
                role="tab"
                type="button"
                aria-selected={selected}
                onClick={() => onChange(id)}
                className={`rounded-full px-4 py-1.5 text-xs font-medium transition-colors ${
                  selected
                    ? 'bg-red text-white'
                    : 'text-muted hover:text-fg'
                }`}
              >
                {c?.shortLabel ?? id}
                {c?.tier === 1 ? (
                  <span
                    className={`ml-1.5 rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${
                      selected ? 'bg-white/20 text-white' : 'bg-mist text-muted'
                    }`}
                  >
                    benchmark
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}
      {arms.length > 1 ? (
        <p className="mt-2 text-xs leading-relaxed text-faint">
          <span className="font-medium text-muted">
            Same inputs, same rings, different structure.
          </span>{' '}
          Tier 1 scores each block on its own; Tier 2 adds a spatial graph so
          nearby and functionally similar blocks can inform each other. Tier 1
          is the benchmark we report: the graph did not improve accuracy.
        </p>
      ) : null}
      {arms.length > 1 && activeConfig?.tier === 2 ? (
        <details className="mt-1 text-xs text-faint">
          <summary className="cursor-pointer select-none hover:text-muted">
            Why two models?
          </summary>
          <p className="mt-1 leading-relaxed">
            The graph was an ablation, not an upgrade: across seeds, no graph
            was the best Tier 2 arm, and no edge family beat the Tier 1
            benchmark. The two tiers are also not yet scored on the same
            population, so we publish no head-to-head accuracy number. What the
            toggle shows is where two differently structured models disagree,
            which reads how much of the estimate is structural rather than
            measured.
          </p>
        </details>
      ) : null}
      {notice ? (
        <p className="mt-2 text-xs text-muted" role="status">
          {notice}
        </p>
      ) : null}
    </div>
  );
}
