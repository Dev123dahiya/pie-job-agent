import clsx from "clsx";
import { Card } from "@/components/ui/Card";
import { limitTone, PLATFORM_LABEL, usedPercent } from "@/lib/home";
import type { DailyLimit, Fetched } from "@/lib/types";

const BAR = { good: "bg-good", warning: "bg-warning", critical: "bg-critical" } as const;

export function DailyLimits({ limits }: { limits: Fetched<DailyLimit[]> }) {
  return (
    <Card title="Today vs daily limits" description="Live submissions only; dry runs never count">
      {!limits.ok ? (
        <p role="alert" className="text-sm text-ink-2">
          Limits unavailable: {limits.error}
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {limits.data.map((l) => (
            <li key={l.platform}>
              <div className="flex items-baseline justify-between text-sm">
                <span className={clsx("text-ink", l.platform === "total" && "font-semibold")}>
                  {PLATFORM_LABEL[l.platform]}
                </span>
                <span className="tabular text-ink-2">
                  {l.used} / {l.limit}
                  {l.remaining === 0 && <span className="ml-2 text-xs font-semibold text-critical">limit reached</span>}
                </span>
              </div>
              <div
                role="progressbar"
                aria-label={`${PLATFORM_LABEL[l.platform]} daily limit`}
                aria-valuemin={0}
                aria-valuemax={l.limit}
                aria-valuenow={l.used}
                className="mt-1 h-2 overflow-hidden rounded-full bg-surface-2"
              >
                <div
                  className={clsx("h-full rounded-full", BAR[limitTone(l)])}
                  style={{ width: `${usedPercent(l)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
