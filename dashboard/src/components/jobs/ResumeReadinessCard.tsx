import { CircleCheck, CircleX, ExternalLink } from "lucide-react";
import Link from "next/link";
import { ExperienceGapNotice } from "@/components/jobs/ExperienceGapNotice";
import { CreateDraftButton, CreateTailoredResumeButton } from "@/components/jobs/JobActions";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { humanize, relativeTime } from "@/lib/format";
import { TAILORABLE_STATES, describeChanges, readinessView } from "@/lib/resume";
import type { JobDetail } from "@/lib/types";

function Check({ ok, label }: { ok: boolean; label: string }) {
  const Icon = ok ? CircleCheck : CircleX;
  return (
    <li className="flex items-center gap-2 text-sm text-ink">
      <Icon aria-hidden className={ok ? "size-4 shrink-0 text-good" : "size-4 shrink-0 text-ink-3"} />
      {label}
      <span className="sr-only">{ok ? "(yes)" : "(no)"}</span>
    </li>
  );
}

/** Resume status and application-draft readiness for one job. Internal only. */
export function ResumeReadinessCard({ job }: { job: JobDetail }) {
  const resume = job.resume;
  const latest = resume?.latest ?? null;
  const readiness = job.readiness;
  const view = readinessView(readiness, job.application !== null);
  const canTailor = TAILORABLE_STATES.has(job.status) && job.analyses.length > 0;
  const count = resume?.tailored_versions.length ?? 0;

  return (
    <Card
      title="Resume & readiness"
      description="Internal only. Nothing is submitted."
      action={<StatusBadge status={view.status} label={view.label} />}
    >
      <div className="flex flex-col gap-4">
        <ul className="flex flex-col gap-1.5">
          <Check ok={Boolean(resume?.master_available)} label="Master resume available (read-only)" />
          <Check
            ok={latest !== null}
            label={latest ? `Tailored resume v${latest.version_number} · ${count} version${count === 1 ? "" : "s"}` : "No tailored resume yet"}
          />
          <Check ok={job.application !== null} label={job.application ? "Application draft exists" : "No application draft"} />
        </ul>

        {job.experience_gap && <ExperienceGapNotice gap={job.experience_gap} />}

        {latest && (
          <div className="rounded-lg border border-border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-medium text-ink">
                v{latest.version_number} · {humanize(latest.tailoring_strategy ?? "balanced")}
              </p>
              <StatusBadge
                status={latest.validation_status === "VALIDATED" ? "ok" : "unknown"}
                label={latest.validation_status === "VALIDATED" ? "Validated" : "Not validated"}
              />
            </div>
            <p className="mt-0.5 text-xs text-ink-3" suppressHydrationWarning>
              Created {relativeTime(latest.created_at)}
            </p>
            {latest.change_details && (
              <ul className="mt-2 list-disc space-y-0.5 pl-4 text-xs text-ink-2">
                {describeChanges(latest.change_details).map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}
            <Link
              href={`/resumes/${latest.id}`}
              className="mt-2 inline-flex items-center gap-1 text-xs text-accent hover:underline"
            >
              View version, report and files <ExternalLink aria-hidden className="size-3" />
            </Link>
          </div>
        )}

        {readiness && readiness.user_blockers.length > 0 && (
          <div role="alert" className="rounded-lg border border-warning/60 bg-warning/10 p-3">
            <p className="text-xs font-semibold uppercase tracking-wider text-ink">Missing information</p>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-sm text-ink">
              {readiness.user_blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </div>
        )}
        {readiness && readiness.pending_steps.length > 0 && (
          <div>
            <p className="text-xs text-ink-3">Before READY_TO_APPLY</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink-2">
              {readiness.pending_steps.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          </div>
        )}

        {(canTailor || (job.status === "SHORTLISTED" && !job.application)) && (
          <div className="flex flex-col gap-3 border-t border-border pt-3">
            {canTailor && <CreateTailoredResumeButton jobId={job.id} hasVersion={latest !== null} />}
            {job.status === "SHORTLISTED" && !job.application && <CreateDraftButton jobId={job.id} />}
          </div>
        )}
      </div>
    </Card>
  );
}
