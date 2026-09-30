import { ArrowLeft, Download } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ExperienceGapNotice } from "@/components/jobs/ExperienceGapNotice";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { getResumeVersion } from "@/lib/api";
import { humanize, relativeTime } from "@/lib/format";
import { artifactUrl, describeChanges } from "@/lib/resume";

export const metadata: Metadata = { title: "Resume version" };
export const dynamic = "force-dynamic";

function List({ title, items, empty = "None" }: { title: string; items: string[]; empty?: string }) {
  return (
    <div>
      <p className="mb-1 text-xs text-ink-3">{title}</p>
      {items.length ? (
        <ul className="list-disc space-y-0.5 pl-5 text-sm text-ink-2">
          {items.map((i) => (
            <li key={i}>{i}</li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-ink-3">{empty}</p>
      )}
    </div>
  );
}

export default async function ResumeVersionPage(props: PageProps<"/resumes/[id]">) {
  const { id } = await props.params;
  const res = await getResumeVersion(id);
  if (!res.ok) {
    if (res.error.includes("404") || res.error.includes("422")) notFound();
    return (
      <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
        {res.error}
      </div>
    );
  }
  const v = res.data;
  const report = v.tailoring_report;
  const changes = v.change_details;
  const rejected = changes?.unsupported_claims_rejected ?? [];

  return (
    <div className="flex flex-col gap-6">
      <Link href="/resumes" className="inline-flex items-center gap-1 text-sm text-ink-2 hover:text-ink">
        <ArrowLeft aria-hidden className="size-4" /> All resumes
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm text-ink-3">{v.company ?? "Unknown company"}</p>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">
            {v.job_title ?? v.role ?? "Resume"} · v{v.version_number}
          </h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink-3">
            <StatusBadge
              status={v.validation_status === "VALIDATED" ? "ok" : "unknown"}
              label={v.validation_status === "VALIDATED" ? "Validated" : "Not validated"}
            />
            <span>{humanize(v.tailoring_strategy ?? "balanced")}</span>
            <span suppressHydrationWarning>· created {relativeTime(v.created_at)}</span>
            {v.job_id && (
              <Link href={`/jobs/${v.job_id}`} className="text-accent hover:underline">
                Source job
              </Link>
            )}
          </div>
          <p className="mt-2 text-xs text-ink-3">
            Derived from the master resume (SHA-256 {v.source_sha256?.slice(0, 12) ?? "—"}…) · {v.model ?? "unknown model"} ·{" "}
            {v.prompt_version ?? "—"}. Internal only. Nothing is submitted.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {v.artifacts.map((format) => (
            <a
              key={format}
              href={artifactUrl(v.id, format)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-2 text-sm font-medium text-ink hover:bg-surface-2"
            >
              <Download aria-hidden className="size-4" /> {format === "pdf" ? "PDF" : "Markdown"}
            </a>
          ))}
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="flex flex-col gap-6 lg:col-span-2">
          <Card title="Resume content" description="Rendered from the validated version">
            {v.markdown ? (
              <pre className="max-h-[70vh] overflow-auto whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-ink">
                {v.markdown}
              </pre>
            ) : (
              <p className="text-sm text-ink-3">No Markdown artifact stored for this version.</p>
            )}
          </Card>
        </div>

        <div className="flex flex-col gap-6">
          {changes && (
            <Card title="Change summary" description="Facts preserved; only order and wording changed">
              <div className="flex flex-col gap-3">
                <List title="What changed" items={describeChanges(changes)} />
                <List title="Keywords emphasised (verified only)" items={changes.keywords_emphasized} />
                <List title="Notes" items={changes.notes} />
              </div>
            </Card>
          )}
          {report && (
            <Card title="Tailoring report" description={report.note}>
              <div className="flex flex-col gap-3">
                {report.experience_gap && <ExperienceGapNotice gap={report.experience_gap} />}
                <List title="Matching verified skills" items={report.matching_verified_skills} />
                <List title="Matching experience" items={report.matching_experience} />
                <List title="Matching projects" items={report.matching_projects} />
                <List title="Gaps (not claimed)" items={report.missing_requirements} />
                <List title="Relevance improvements" items={report.relevance_improvements} />
              </div>
            </Card>
          )}
          <Card title="Validation" description={`${v.validation_report?.attempts ?? 1} attempt(s)`}>
            <div className="flex flex-col gap-3">
              <List title="Checks passed" items={v.validation_report?.checks ?? []} />
              <List
                title="Unsupported claims rejected before saving"
                items={rejected.map((r) => `${humanize(r.code)}: ${r.detail}`)}
              />
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
