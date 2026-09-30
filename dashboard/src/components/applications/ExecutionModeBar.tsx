import clsx from "clsx";
import { OctagonAlert, ShieldCheck, TriangleAlert } from "lucide-react";
import { liveBlockReason, modeChips } from "@/lib/execution";
import type { ExecutionModeStatus } from "@/lib/types";

const ICON = { live: OctagonAlert, warning: TriangleAlert, safe: ShieldCheck } as const;

/** The current execution mode, shown before any application execution. Never hidden. */
export function ExecutionModeBar({ mode }: { mode: ExecutionModeStatus | null }) {
  const chips = modeChips(mode);
  const live = chips.some((c) => c.tone === "live");
  const reason = mode ? liveBlockReason(mode) : null;
  return (
    <div
      role={live ? "alert" : "status"}
      aria-label="Execution mode"
      className={clsx(
        "flex flex-wrap items-center gap-2 rounded-xl border-2 px-3 py-2",
        live ? "border-critical bg-critical/10" : "border-border bg-surface",
      )}
    >
      {chips.map((c) => {
        const Icon = ICON[c.tone];
        return (
          <span
            key={c.label}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-bold tracking-wide",
              c.tone === "live" && "border-critical bg-critical text-white",
              c.tone === "warning" && "border-warning/70 bg-warning/10 text-ink",
              c.tone === "safe" && "border-accent/50 bg-accent/5 text-ink",
            )}
          >
            <Icon aria-hidden className="size-3.5" />
            {c.label}
          </span>
        );
      })}
      {reason && <span className="text-xs text-ink-2">{reason}</span>}
      {live && (
        <span className="text-xs font-medium text-critical">
          Live runs act on real employer websites. Each submission still needs your confirmation.
        </span>
      )}
    </div>
  );
}
