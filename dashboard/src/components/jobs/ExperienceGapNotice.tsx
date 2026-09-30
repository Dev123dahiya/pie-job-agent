import { StatusBadge } from "@/components/ui/StatusBadge";
import { experienceGapView } from "@/lib/resume";
import type { ExperienceGap } from "@/lib/types";

/**
 * The experience gap, stated plainly. Tailoring improves relevance of documented
 * qualifications; it cannot close this gap, and shortlist eligibility is decided separately.
 */
export function ExperienceGapNotice({ gap }: { gap: ExperienceGap }) {
  const view = experienceGapView(gap);
  return (
    <div
      role={gap.gap === true ? "note" : undefined}
      className="rounded-lg border border-border p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-semibold uppercase tracking-wider text-ink">Experience</p>
        <StatusBadge status={view.status} label={view.label} />
      </div>
      <p className="mt-1.5 text-sm text-ink">{view.detail}</p>
      {gap.internships.length > 0 && (
        <p className="mt-1 text-xs text-ink-2">
          Internships ({gap.internships.length}) are listed on the resume but not counted as full-time experience.
        </p>
      )}
      <ul className="mt-2 list-disc space-y-0.5 pl-4 text-xs text-ink-3">
        <li>Resume tailoring improves the relevance of documented qualifications only.</li>
        <li>Shortlist eligibility is decided separately by the job-analysis rules.</li>
        <li>An experience gap is a factual constraint and is never concealed.</li>
      </ul>
    </div>
  );
}
