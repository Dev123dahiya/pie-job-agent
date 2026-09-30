import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { AnalyzeJobButton, CreateDraftButton } from "@/components/jobs/JobActions";
import { ANALYZABLE_STATES } from "@/lib/jobs";
import { MatchDimensions } from "@/components/jobs/MatchDimensions";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { FRESHNESS_LABEL, POSTED_KIND_LABEL, formatSalary, humanize, relativeTime } from "@/lib/format";
import type { JobSummary } from "@/lib/types";

const TH = "px-3 py-2 text-left text-xs font-medium text-ink-3 whitespace-nowrap";
const TD = "px-3 py-3 align-top";

export function JobsTable({ jobs, emptyMessage }: { jobs: JobSummary[]; emptyMessage: string }) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-ink-2">
        {emptyMessage}
      </div>
    );
  }
  return (
    // Horizontal scroll stays inside the card; the page itself never scrolls sideways.
    <div className="overflow-x-auto rounded-xl border border-border bg-surface">
      <table className="w-full min-w-[980px] border-collapse text-sm">
        <thead className="border-b border-border bg-surface-2/50">
          <tr>
            <th scope="col" className={TH}>Company · Role</th>
            <th scope="col" className={TH}>Location</th>
            <th scope="col" className={TH}>Salary</th>
            <th scope="col" className={TH}>Posted</th>
            <th scope="col" className={TH}>Source</th>
            <th scope="col" className={TH}>Match</th>
            <th scope="col" className={TH}>Status</th>
            <th scope="col" className={TH}>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {jobs.map((job) => (
            <tr key={job.id} className="hover:bg-surface-2/40">
              <td className={`${TD} max-w-[280px]`}>
                <p className="text-xs text-ink-3">{job.company ?? "Unknown company"}</p>
                <Link href={`/jobs/${job.id}`} className="font-medium text-ink hover:text-accent hover:underline">
                  {job.title}
                </Link>
                {job.experience_required && (
                  <p className="mt-0.5 text-xs text-ink-3">Asks for {job.experience_required}</p>
                )}
              </td>
              <td className={`${TD} max-w-[180px] text-ink-2`}>
                <span className="line-clamp-2">{job.location ?? "Not listed"}</span>
                {job.remote_type !== "unknown" && (
                  <span className="text-xs text-ink-3">{humanize(job.remote_type)}</span>
                )}
              </td>
              <td className={`${TD} tabular whitespace-nowrap text-ink-2`}>
                {formatSalary(job.salary_min, job.salary_max, job.salary_currency)}
                {job.salary_period === "MONTH" && <p className="text-xs text-ink-3">from a monthly figure ×12</p>}
                {job.salary_note && <p className="max-w-[160px] whitespace-normal text-xs text-ink-3">{job.salary_note}</p>}
              </td>
              <td className={`${TD} whitespace-nowrap`}>
                <p className="text-ink-2" suppressHydrationWarning>
                  {job.posted_at ? relativeTime(job.posted_at) : "—"}
                </p>
                <StatusBadge status={job.freshness} label={FRESHNESS_LABEL[job.freshness]} className="mt-1" />
                {job.posted_at_kind && job.posted_at_kind !== "published" && (
                  <p className="mt-1 max-w-[150px] whitespace-normal text-xs text-ink-3">
                    {POSTED_KIND_LABEL[job.posted_at_kind] ?? job.posted_at_kind}
                  </p>
                )}
                <p className="mt-1 text-xs text-ink-3" suppressHydrationWarning>
                  Found {relativeTime(job.discovered_at)}
                </p>
              </td>
              <td className={`${TD} text-ink-2`}>
                <a href={job.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-accent">
                  {job.source}
                  <ExternalLink aria-hidden className="size-3" />
                  <span className="sr-only">(opens the original posting)</span>
                </a>
                {job.also_on && job.also_on.length > 0 && (
                  <p className="text-xs text-ink-3">also on {job.also_on.join(", ")}</p>
                )}
              </td>
              <td className={TD}>
                <MatchDimensions analysis={job.analysis} />
              </td>
              <td className={TD}>
                <StatusBadge status={job.status} />
                {job.tailored_version != null && (
                  <p className="mt-1 text-xs text-ink-2">Tailored resume v{job.tailored_version}</p>
                )}
                {job.last_error && (
                  <p className="mt-1 max-w-[180px] text-xs text-critical" title={job.last_error}>
                    Last analysis failed
                  </p>
                )}
              </td>
              <td className={TD}>
                <div className="flex flex-col gap-2">
                  {ANALYZABLE_STATES.has(job.status) && (
                    <AnalyzeJobButton jobId={job.id} analyzed={job.analysis !== null} />
                  )}
                  {job.status === "SHORTLISTED" && <CreateDraftButton jobId={job.id} />}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
