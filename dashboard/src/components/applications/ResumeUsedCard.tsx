import { Lock } from "lucide-react";
import Link from "next/link";
import { Card } from "@/components/ui/Card";
import type { ApplicationDetail } from "@/lib/types";

/** The resume this application uses. Locked once it may be with the employer (B5). */
export function ResumeUsedCard({ resume }: { resume: ApplicationDetail["resume"] }) {
  return (
    <Card title="Resume used">
      {resume === null ? (
        <p className="text-sm text-ink-2">No resume linked yet. A validated tailored resume is linked when it is made.</p>
      ) : (
        <div className="flex flex-col gap-1.5 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/resumes/${resume.version_id}`} className="font-medium text-ink hover:underline">
              {resume.is_master ? "Master resume" : `Tailored v${resume.version_number}`}
            </Link>
            {resume.locked && (
              <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-2 px-2 py-0.5 text-xs font-semibold text-ink">
                <Lock aria-hidden className="size-3" /> Locked
              </span>
            )}
          </div>
          <p className="break-all text-xs text-ink-3">{resume.file_name}</p>
          {resume.lock_reason && <p className="text-xs text-ink-2">{resume.lock_reason}</p>}
        </div>
      )}
    </Card>
  );
}
