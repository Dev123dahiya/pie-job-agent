import { percent } from "@/lib/format";
import type { FunnelStage } from "@/lib/types";

/**
 * Application funnel as horizontal bars (single series, one hue).
 * Every bar is directly labelled with its count, so no legend is needed;
 * hovering or focusing a row shows the stage-to-stage conversion.
 */
export function Funnel({ stages }: { stages: FunnelStage[] }) {
  const max = Math.max(1, ...stages.map((s) => s.count));

  return (
    <ol className="flex flex-col gap-2.5" aria-label="Application funnel">
      {stages.map((stage, i) => {
        const prev = i > 0 ? stages[i - 1] : undefined;
        const width = (stage.count / max) * 100;
        const conversion = prev ? `${percent(stage.count, prev.count)} of ${prev.stage.toLowerCase()}` : "Top of funnel";
        return (
          <li
            key={stage.stage}
            tabIndex={0}
            className="group relative grid grid-cols-[6.5rem_1fr_3rem] items-center gap-3 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-accent"
            aria-label={`${stage.stage}: ${stage.count}. ${conversion}`}
          >
            <span className="text-sm text-ink-2">{stage.stage}</span>
            <span className="relative h-7 rounded-r-[4px] bg-surface-2">
              <span
                data-testid="funnel-bar"
                className="absolute inset-y-0 left-0 rounded-r-[4px] bg-series-1 transition-[width] duration-500 group-hover:brightness-110"
                style={{ width: stage.count > 0 ? `max(${width}%, 3px)` : "0%" }}
              />
              <span
                role="tooltip"
                className="pointer-events-none absolute -top-9 left-2 z-10 hidden whitespace-nowrap rounded-md border border-border bg-surface px-2 py-1 text-xs text-ink shadow-sm group-hover:block group-focus-visible:block"
              >
                <span className="tabular font-semibold">{stage.count}</span>{" "}
                <span className="text-ink-2">{stage.stage.toLowerCase()} · {conversion}</span>
              </span>
            </span>
            <span className="tabular text-right text-sm font-semibold text-ink">{stage.count}</span>
          </li>
        );
      })}
    </ol>
  );
}
