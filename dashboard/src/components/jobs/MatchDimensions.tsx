import clsx from "clsx";
import { CircleCheck, CircleHelp, CircleMinus, CircleX, type LucideIcon } from "lucide-react";
import type { Analysis, Level } from "@/lib/types";

export const DIMENSIONS = [
  { key: "role_match", short: "Role", label: "Role" },
  { key: "skills_match", short: "Skills", label: "Skills" },
  { key: "experience_match", short: "Exp", label: "Experience" },
  { key: "location_match", short: "Loc", label: "Location" },
  { key: "salary_match", short: "Pay", label: "Salary" },
] as const;

// Shape + color + text: a level is never shown by color alone.
export const LEVEL_STYLE: Record<Level, { icon: LucideIcon; color: string }> = {
  strong: { icon: CircleCheck, color: "text-good" },
  medium: { icon: CircleMinus, color: "text-warning" },
  weak: { icon: CircleX, color: "text-critical" },
  unknown: { icon: CircleHelp, color: "text-neutral" },
};

/** Compact per-dimension view for tables; the score is decision support, not a verdict. */
export function MatchDimensions({ analysis }: { analysis: Analysis | null }) {
  if (!analysis) {
    return <span className="text-xs text-ink-3">Not analyzed</span>;
  }
  return (
    <div className="flex flex-col gap-1">
      <span className="tabular text-sm font-semibold text-ink">
        {analysis.match_score ?? "—"}
        <span className="ml-1 text-xs font-normal text-ink-3">/100</span>
      </span>
      <ul className="flex flex-wrap gap-x-2 gap-y-0.5" aria-label="Match dimensions">
        {DIMENSIONS.map(({ key, short, label }) => {
          const level = analysis[key];
          const { icon: Icon, color } = LEVEL_STYLE[level];
          return (
            <li key={key} className="flex items-center gap-0.5 text-[11px] text-ink-2" title={`${label}: ${level}`}>
              <Icon aria-hidden className={clsx("size-3", color)} />
              <span>{short}</span>
              <span className="sr-only">: {level}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
