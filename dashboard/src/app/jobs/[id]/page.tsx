import clsx from "clsx";
import { ArrowLeft, CircleCheck, CircleX, ExternalLink, TriangleAlert } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { AutomationNotice } from "@/components/jobs/AutomationNotice";
import { AnalyzeJobButton, ManualApplyButton, ShortlistJobButton, SkipJobButton } from "@/components/jobs/JobActions";
import { APPLY_TYPE_LABEL, PLATFORM_LABEL, STATUS_LABEL } from "@/lib/home";
import { ResumeReadinessCard } from "@/components/jobs/ResumeReadinessCard";
import { ANALYZABLE_STATES, MANUAL_ELIGIBLE_STATES, SHORTLISTABLE_STATES, SKIPPABLE_STATES } from "@/lib/jobs";
import { DIMENSIONS, LEVEL_STYLE } from "@/components/jobs/MatchDimensions";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { FRESHNESS_LABEL, formatSalary, humanize, relativeTime } from "@/lib/format";
import { getJob } from "@/lib/api";
import type { Analysis, JobDetail } from "@/lib/types";

export const metadata: Metadata = { title: "Job" };
export const dynamic = "force-dynamic";

function Chips({ items, tone }: { items: string[]; tone: "good" | "missing" }) {
  if (items.length === 0) return <p className="text-sm text-ink-3">None</p>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((s) => (
        <li
          key={s}
          className={clsx(
            "rounded-md border px-2 py-0.5 text-xs",
            tone === "good" ? "border-border bg-surface-2 text-ink" : "border-dashed border-border text-ink-2",
          )}
        >
          {s}
        </li>
      ))}
    </ul>
  );
}

function AnalysisCard({ a, total }: { a: Analysis; total: number }) {
  const heuristic = a.analyzer === "heuristic" || a.analyzer === "mock";
  return (
    <Card
      title="Match analysis"
      description={`${heuristic ? "Heuristic (not Claude)" : "Claude"} · ${a.model ?? "unknown model"} · ${a.prompt_version ?? "—"}${total > 1 ? ` · ${total} analyses` : ""}`}
      action={
        <StatusBadge
          status={a.shortlisted ? "SHORTLISTED" : "REJECTED_BY_AGENT"}
          label={a.shortlisted ? "Shortlisted" : "Not shortlisted"}
        />
      }
    >
      <div className="flex flex-col gap-5">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {DIMENSIONS.map(({ key, label }) => {
            const level = a[key];
            const { icon: Icon, color } = LEVEL_STYLE[level];
            return (
              <div key={key} className="rounded-lg border border-border px-3 py-2">
                <p className="text-xs text-ink-3">{label}</p>
                <p className="mt-0.5 flex items-center gap-1.5 text-sm font-medium capitalize text-ink">
                  <Icon aria-hidden className={clsx("size-4", color)} />
                  {level}
                </p>
              </div>
            );
          })}
          <div className="rounded-lg border border-border px-3 py-2">
            <p className="text-xs text-ink-3">Freshness</p>
            <p className="mt-0.5 text-sm font-medium text-ink">{FRESHNESS_LABEL[a.freshness]}</p>
          </div>
        </div>

        <div>
          <p className="text-xs text-ink-3">
            Match score <span className="text-ink-3">(decision support only)</span>
          </p>
          <p className="tabular text-2xl font-semibold text-ink">
            {a.match_score ?? "—"}
            <span className="text-sm font-normal text-ink-3">/100</span>
          </p>
        </div>

        {a.reason && (
          <div>
            <p className="mb-1 text-xs text-ink-3">
              Reasoning · analyzer {a.should_shortlist ? "recommends" : "does not recommend"} shortlisting
            </p>
            <p className="text-sm leading-relaxed text-ink">{a.reason}</p>
          </div>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <p className="mb-1.5 text-xs text-ink-3">Matching skills (from your verified profile)</p>
            <Chips items={a.matching_skills} tone="good" />
          </div>
          <div>
            <p className="mb-1.5 text-xs text-ink-3">Missing skills</p>
            <Chips items={a.missing_skills} tone="missing" />
          </div>
        </div>

        {a.concerns.length > 0 && (
          <div>
            <p className="mb-1.5 text-xs text-ink-3">Concerns</p>
            <ul className="space-y-1.5 text-sm text-ink-2">
              {a.concerns.map((c) => (
                <li key={c} className="flex gap-2">
                  <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0 text-serious" />
                  {c}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div>
          <p className="mb-1.5 text-xs text-ink-3">Shortlist rules (config/shortlist_rules.yaml)</p>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {a.rule_results.map((r) => (
              <li key={r.rule} className="flex items-center justify-between gap-3 px-3 py-1.5 text-sm">
                <span className="flex items-center gap-2 text-ink">
                  {r.passed ? (
                    <CircleCheck aria-hidden className="size-4 text-good" />
                  ) : (
                    <CircleX aria-hidden className="size-4 text-critical" />
                  )}
                  {humanize(r.rule)}
                  <span className="sr-only">{r.passed ? "passed" : "failed"}</span>
                </span>
                <span className="text-right text-xs text-ink-3">{r.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Card>
  );
}

function Facts({ job }: { job: JobDetail }) {
  const rows: [string, React.ReactNode][] = [
    ["Salary", formatSalary(job.salary_min, job.salary_max, job.salary_currency)],
    ["Experience asked", job.experience_required ?? "Not stated"],
    ["Location", job.location ?? "Not listed"],
    ["Work mode", humanize(job.remote_type)],
    ["Posted", job.posted_at ? relativeTime(job.posted_at) : "No post date"],
    ["Discovered", relativeTime(job.discovered_at)],
    ["Source", `${job.source} · seen ${job.times_seen}×`],
  ];
  return (
    <Card title="Posting facts" description="Only what the posting states">
      <dl className="divide-y divide-border">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-4 py-2 text-sm">
            <dt className="text-ink-3">{k}</dt>
            <dd className="text-right text-ink" suppressHydrationWarning>
              {v}
            </dd>
          </div>
        ))}
      </dl>
      {job.skills.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 text-xs text-ink-3">Skills mentioned</p>
          <Chips items={job.skills} tone="good" />
        </div>
      )}
    </Card>
  );
}

export default async function JobPage(props: PageProps<"/jobs/[id]">) {
  const { id } = await props.params;
  const res = await getJob(id);
  if (!res.ok) {
    if (res.error.includes("404") || res.error.includes("422")) notFound();
    return (
      <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
        {res.error}
      </div>
    );
  }
  const job = res.data;
  const latest = job.analyses[0] ?? null;

  return (
    <div className="flex flex-col gap-6">
      <Link href="/jobs" className="inline-flex items-center gap-1 text-sm text-ink-2 hover:text-ink">
        <ArrowLeft aria-hidden className="size-4" /> All jobs
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm text-ink-3">{job.company ?? "Unknown company"}</p>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">{job.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-ink">{STATUS_LABEL[job.user_status]}</span>
            <StatusBadge status={job.status} />
            <StatusBadge status={job.freshness} label={FRESHNESS_LABEL[job.freshness]} />
            <a
              href={job.url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sm text-accent hover:underline"
            >
              View original posting <ExternalLink aria-hidden className="size-3.5" />
            </a>
          </div>
          <p className="mt-2 text-xs text-ink-3">
            {PLATFORM_LABEL[job.platform]} · {APPLY_TYPE_LABEL[job.apply_type]} · from {job.source}
            {(job.also_on?.length ?? 0) > 0 && <> · also found on {job.also_on?.join(", ")} (duplicate, applied to once)</>}
          </p>
          {job.last_error && <p className="mt-2 text-sm text-critical">{job.last_error}</p>}
        </div>
        <div className="flex flex-col items-start gap-2">
          {ANALYZABLE_STATES.has(job.status) && (
            <AnalyzeJobButton jobId={job.id} analyzed={latest !== null} />
          )}
          {SHORTLISTABLE_STATES.has(job.status) && <ShortlistJobButton jobId={job.id} />}
          {SKIPPABLE_STATES.has(job.status) && <SkipJobButton jobId={job.id} />}
          {MANUAL_ELIGIBLE_STATES.has(job.status) && <ManualApplyButton jobId={job.id} />}
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="flex flex-col gap-6 lg:col-span-2">
          {latest ? (
            <AnalysisCard a={latest} total={job.analyses.length} />
          ) : (
            <Card title="Match analysis">
              <p className="text-sm text-ink-2">Not analyzed yet. Use Analyze to run Claude on this posting.</p>
            </Card>
          )}
          <Card title="Description" description="Extracted from the posting">
            {job.requirements.length > 0 && (
              <div className="mb-4">
                <p className="mb-1.5 text-xs text-ink-3">Requirements</p>
                <ul className="list-disc space-y-1 pl-5 text-sm text-ink-2">
                  {job.requirements.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </div>
            )}
            {job.description ? (
              <details>
                <summary className="cursor-pointer text-sm text-accent">Show full description</summary>
                <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-ink-2">{job.description}</p>
              </details>
            ) : (
              <p className="text-sm text-ink-3">The posting has no description.</p>
            )}
          </Card>
        </div>

        <div className="flex flex-col gap-6">
          <ResumeReadinessCard job={job} />
          <Facts job={job} />
          {job.application && (
            <Card title="Application" description="Draft and browser execution">
              <div className="flex flex-col gap-2 text-sm">
                <StatusBadge status={job.application.status} />
                <Link href={`/applications/${job.application.id}`} className="text-accent hover:underline">
                  Open application execution (dry run / status)
                </Link>
                {job.application.user_action_required && (
                  <div role="alert" className="rounded-lg border border-warning/60 bg-warning/10 p-3">
                    <p className="text-xs font-semibold uppercase tracking-wider text-ink">Needs you</p>
                    <ul className="mt-1 list-disc space-y-1 pl-4 text-ink">
                      {job.application.user_action_required.split("\n").map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </Card>
          )}
          <AutomationNotice />
          <Card title="Timeline">
            <ol className="space-y-3">
              {job.timeline.map((e, i) => (
                <li key={`${e.occurred_at}-${i}`} className="text-sm">
                  <p className="text-ink">{humanize(e.event_type)}</p>
                  {e.reason && <p className="text-xs text-ink-2">{e.reason}</p>}
                  <time dateTime={e.occurred_at} className="text-xs text-ink-3" suppressHydrationWarning>
                    {relativeTime(e.occurred_at)}
                  </time>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
    </div>
  );
}
