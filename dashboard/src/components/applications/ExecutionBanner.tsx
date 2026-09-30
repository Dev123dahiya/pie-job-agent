import clsx from "clsx";
import { OctagonAlert, ShieldCheck } from "lucide-react";
import { modeView } from "@/lib/execution";

/** Full-width mode banner. LIVE is loud and red; DRY RUN is calm - never ambiguous. */
export function ExecutionBanner({ mode }: { mode: "DRY_RUN" | "LIVE" }) {
  const view = modeView(mode);
  const Icon = view.live ? OctagonAlert : ShieldCheck;
  return (
    <div
      role={view.live ? "alert" : "status"}
      data-mode={mode}
      className={clsx(
        "flex items-start gap-3 rounded-xl border-2 px-4 py-3",
        view.live ? "border-critical bg-critical/10" : "border-accent/60 bg-accent/5",
      )}
    >
      <Icon aria-hidden className={clsx("mt-0.5 size-5 shrink-0", view.live ? "text-critical" : "text-accent")} />
      <div>
        <p className={clsx("text-sm font-bold tracking-wide", view.live ? "text-critical" : "text-ink")}>{view.title}</p>
        <p className="mt-0.5 text-xs text-ink-2">{view.detail}</p>
      </div>
    </div>
  );
}
