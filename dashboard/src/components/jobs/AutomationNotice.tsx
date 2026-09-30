import { Lock } from "lucide-react";

/** How application automation behaves. There is deliberately no one-click Apply button here. */
export function AutomationNotice() {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-dashed border-border bg-surface px-4 py-3">
      <Lock aria-hidden className="mt-0.5 size-4 shrink-0 text-ink-3" />
      <div>
        <p className="text-xs font-semibold uppercase tracking-wider text-ink">Application automation</p>
        <p className="text-xs text-ink-2">
          READY_TO_APPLY jobs can be run in the browser as a DRY RUN from their application page: PIE fills
          verified answers and stops before the final submit. Live submission is off unless you enable it, and
          then needs your explicit confirmation for every application.
        </p>
      </div>
    </div>
  );
}
