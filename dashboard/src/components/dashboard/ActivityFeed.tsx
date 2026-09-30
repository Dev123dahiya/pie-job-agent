import clsx from "clsx";
import { Card } from "@/components/ui/Card";
import { StatusBadge, toneFor } from "@/components/ui/StatusBadge";
import { humanize, relativeTime } from "@/lib/format";
import type { ActivityItem } from "@/lib/types";

const DOT: Record<string, string> = {
  good: "bg-good",
  warning: "bg-warning",
  serious: "bg-serious",
  critical: "bg-critical",
  info: "bg-accent",
  neutral: "bg-neutral",
};

export function ActivityFeed({ items }: { items: ActivityItem[] }) {
  return (
    <Card title="Recent activity" description="Latest timeline events across all applications">
      {items.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-3">No activity yet.</p>
      ) : (
        <ol className="relative">
          {items.map((item, i) => (
            <li key={`${item.occurred_at}-${i}`} className="relative flex gap-3 pb-4 last:pb-0">
              {i < items.length - 1 && (
                <span aria-hidden className="absolute left-[4px] top-4 h-full w-px bg-border" />
              )}
              <span
                aria-hidden
                className={clsx(
                  "relative mt-1.5 size-[9px] shrink-0 rounded-full ring-2 ring-surface",
                  DOT[toneFor(item.new_status ?? "")],
                )}
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <p className="text-sm text-ink">{item.reason ?? humanize(item.event_type)}</p>
                  {item.new_status && <StatusBadge status={item.new_status} />}
                </div>
                <time dateTime={item.occurred_at} className="text-xs text-ink-3">
                  {relativeTime(item.occurred_at)}
                </time>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
