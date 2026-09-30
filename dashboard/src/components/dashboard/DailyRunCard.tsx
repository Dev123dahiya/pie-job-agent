import Link from "next/link";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { relativeTime } from "@/lib/format";
import type { DailyJob, DailySchedule, Fetched } from "@/lib/types";

const COUNTS: [keyof NonNullable<NonNullable<DailySchedule["latest"]>["report"]>["counts"], string][] = [
  ["discovered", "Discovered"],
  ["new", "New"],
  ["duplicates", "Duplicates"],
  ["fresh", "Fresh postings"],
  ["new_fresh_jobs", "New fresh jobs"],
  ["stale", "Stale"],
  ["unknown", "Unknown date"],
  ["analyzed", "Analyzed"],
  ["shortlisted", "Shortlisted"],
  ["ready_to_apply", "Ready to apply"],
  ["failures", "Failures"],
];

function JobList({ title, jobs }: { title: string; jobs: DailyJob[] }) {
  return (
    <div>
      <h3 className="text-xs font-medium text-ink-2">
        {title} ({jobs.length})
      </h3>
      {jobs.length === 0 ? (
        <p className="mt-1 text-xs text-ink-3">None in this run.</p>
      ) : (
        <ul className="mt-1 flex flex-col gap-1">
          {jobs.map((j) => (
            <li key={j.job_id} className="text-sm">
              <Link href={`/jobs/${j.job_id}`} className="text-ink hover:underline">
                {j.title}
              </Link>
              <span className="text-ink-3"> · {j.company ?? "Unknown company"}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Daily discovery + analysis: when it runs next and what the latest run found. */
export function DailyRunCard({ daily }: { daily: Fetched<DailySchedule> }) {
  if (!daily.ok) {
    return (
      <Card title="Daily run" description="Scheduled discovery + analysis">
        <p className="text-sm text-ink-2">{daily.error}</p>
      </Card>
    );
  }
  const { enabled, time, timezone, next_run_at, latest } = daily.data;
  const schedule = enabled
    ? `Every day at ${time} (${timezone})${next_run_at ? ` · next ${relativeTime(next_run_at)}` : ""}`
    : "Schedule off (DAILY_RUN_ENABLED=false)";
  const report = latest?.report ?? null;

  return (
    <Card
      title="Daily run"
      description={`${schedule}. Discovery and analysis only; nothing is applied to.`}
      action={latest ? <StatusBadge status={latest.status} /> : undefined}
    >
      {!latest ? (
        <p className="text-sm text-ink-2">
          No daily run yet. Trigger one with{" "}
          <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-xs">python -m backend.scripts.daily run</code>.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <p className="text-xs text-ink-3">
            {latest.trigger === "scheduled" ? "Scheduled" : "Manual"} run, started {relativeTime(latest.started_at)}
          </p>
          {report ? (
            <>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-5">
                {COUNTS.map(([key, label]) => (
                  <div key={key}>
                    <dt className="text-xs text-ink-3">{label}</dt>
                    <dd className="text-sm font-semibold tabular-nums text-ink">{report.counts[key] ?? "—"}</dd>
                  </div>
                ))}
              </dl>
              <JobList title="Newly shortlisted" jobs={report.newly_shortlisted} />
              <JobList title="Newly ready to apply" jobs={report.newly_ready_to_apply} />
              {report.failures.length > 0 && (
                <ul className="flex flex-col gap-1">
                  {report.failures.map((f, i) => (
                    <li key={i} className="text-xs text-ink-2">
                      <span className="font-medium">{f.step}:</span> {f.error}
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : (
            <p className="text-sm text-ink-2">{latest.summary ?? "The run has not finished yet."}</p>
          )}
        </div>
      )}
    </Card>
  );
}
