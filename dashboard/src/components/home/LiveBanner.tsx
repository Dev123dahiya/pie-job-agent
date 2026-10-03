import { OctagonAlert } from "lucide-react";

/** Shown only when live submissions are switched on (REQUIREMENTS 19.2.1). */
export function LiveBanner({ livePossible, note }: { livePossible: boolean; note: string }) {
  if (!livePossible) return null;
  return (
    <div role="alert" className="flex items-start gap-3 rounded-xl border-2 border-critical bg-critical/10 px-4 py-3">
      <OctagonAlert aria-hidden className="mt-0.5 size-5 shrink-0 text-critical" />
      <div>
        <p className="text-sm font-bold tracking-wide text-critical">LIVE MODE IS ON</p>
        <p className="mt-0.5 text-xs text-ink-2">
          {note} Real applications can be sent. Each one still needs your confirmation and stays within the daily
          limits.
        </p>
      </div>
    </div>
  );
}
