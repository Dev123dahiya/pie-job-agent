import { Card } from "@/components/ui/Card";
import { loginText, PLATFORM_LABEL } from "@/lib/home";
import type { PlatformMode } from "@/lib/types";

const MODE_LABEL = { "dry-run": "Dry run", review: "Review", auto: "Auto" } as const;

export function PlatformModes({ platforms, livePossible }: { platforms: PlatformMode[]; livePossible: boolean }) {
  return (
    <Card
      title="Mode per platform"
      description={livePossible ? "Live switch is ON" : "Nothing is submitted: dry run everywhere"}
    >
      <ul className="divide-y divide-border">
        {platforms.map((p) => (
          <li key={p.platform} className="flex flex-wrap items-start justify-between gap-2 py-2.5 first:pt-0 last:pb-0">
            <div className="min-w-0">
              <p className="text-sm font-medium text-ink">{PLATFORM_LABEL[p.platform]}</p>
              <p className="text-xs text-ink-3">Login: {loginText(p)}</p>
              {p.note && <p className="text-xs text-ink-3">{p.note}</p>}
            </div>
            <span
              data-mode={p.mode}
              className="rounded-full border border-border bg-surface-2 px-2.5 py-0.5 text-xs font-semibold text-ink"
            >
              {MODE_LABEL[p.mode]}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
