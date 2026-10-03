import clsx from "clsx";
import { ArrowLeft, CircleCheck, CircleDashed, TriangleAlert } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ResumeUsedCard } from "@/components/applications/ResumeUsedCard";
import { ExecutionBanner } from "@/components/applications/ExecutionBanner";
import { ExecutionModeBar } from "@/components/applications/ExecutionModeBar";
import { AnswerQuestions, LiveSubmit, SessionActions, StartExecution } from "@/components/applications/ExecutionControls";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { getApplication } from "@/lib/api";
import { screenshotUrl, sessionSteps } from "@/lib/execution";
import { humanize, relativeTime } from "@/lib/format";
import type { ApplicationDetail, BrowserSessionView } from "@/lib/types";

export const metadata: Metadata = { title: "Application execution" };
export const dynamic = "force-dynamic";

function Session({ app, s }: { app: ApplicationDetail; s: BrowserSessionView }) {
  return (
    <div className="flex flex-col gap-4">
      <ExecutionBanner mode={s.mode} />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge status={s.state} />
        <span className="text-ink-2">{s.current_step}</span>
        <span className="text-xs text-ink-3">· {s.target_domain}</span>
        {s.blocked_requests > 0 && (
          <span className="text-xs text-ink-3">· {s.blocked_requests} outgoing request(s) blocked by the dry-run guard</span>
        )}
      </div>

      <ol className="grid gap-1.5 sm:grid-cols-2">
        {sessionSteps(s).map((step) => (
          <li key={step.label} className="flex items-center gap-2 text-sm text-ink">
            {step.done ? (
              <CircleCheck aria-hidden className="size-4 text-good" />
            ) : (
              <CircleDashed aria-hidden className="size-4 text-ink-3" />
            )}
            {step.label}
            <span className="sr-only">{step.done ? "(done)" : "(not done)"}</span>
          </li>
        ))}
      </ol>

      {s.challenges.length > 0 && (
        <div role="alert" className="rounded-lg border border-warning/60 bg-warning/10 p-3">
          <p className="text-xs font-semibold uppercase tracking-wider text-ink">Verification required - PIE never bypasses it</p>
          <ul className="mt-1 space-y-1 text-sm text-ink">
            {s.challenges.map((c) => (
              <li key={c.type}>
                <span className="font-medium">{humanize(c.type)}</span>: {c.detail}. {c.user_action}
              </li>
            ))}
          </ul>
        </div>
      )}

      {s.missing_answers.length > 0 && s.state === "WAITING_FOR_USER" && (
        <div className="rounded-lg border border-warning/60 p-3">
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink">
            Questions without a verified answer - PIE will not guess
          </p>
          <AnswerQuestions appId={app.id} missing={s.missing_answers} />
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <div>
          <p className="mb-1 text-xs text-ink-3">Fields detected ({s.fields.length})</p>
          <ul className="space-y-0.5 text-xs text-ink-2">
            {s.fields.map((f) => (
              <li key={f.field_id} className={clsx(f.action === "block" && "text-critical")}>
                {f.label || f.field_id}
                {f.required && " *"} → {humanize(f.category)} ({f.action})
              </li>
            ))}
          </ul>
        </div>
        <div>
          <p className="mb-1 text-xs text-ink-3">Filled with verified answers ({s.filled_fields.length})</p>
          <ul className="space-y-0.5 text-xs text-ink-2">
            {s.filled_fields.map((f) => (
              <li key={f.field_id}>
                {f.label} — Filled from: {f.key}
                {f.method && f.method !== "direct" ? ` (${f.method})` : ""}
              </li>
            ))}
          </ul>
          {s.uploaded_resume && (
            <p className="mt-2 text-xs text-ink-2">
              Resume: {s.uploaded_resume.file_name} (tailored v{s.uploaded_resume.version_number})
              {s.uploaded_resume.status && (
                <span className="block font-medium text-ink">
                  {humanize(s.uploaded_resume.status)}
                  {s.uploaded_resume.detail ? `: ${s.uploaded_resume.detail}` : ""}
                </span>
              )}
            </p>
          )}
        </div>
        <div>
          <p className="mb-1 text-xs text-ink-3">Skipped ({s.skipped_fields.length})</p>
          <ul className="space-y-0.5 text-xs text-ink-2">
            {s.skipped_fields.map((f) => (
              <li key={f.field_id}>
                {f.label}: {f.reason}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {s.errors.length > 0 && (
        <div>
          <p className="mb-1 text-xs text-ink-3">Errors</p>
          <ul className="space-y-0.5 text-xs text-critical">
            {s.errors.map((e, i) => (
              <li key={`${e.step}-${i}`}>
                {e.step}: {e.error}
              </li>
            ))}
          </ul>
        </div>
      )}

      {s.submission_evidence && (
        <div className="rounded-lg border border-border p-3">
          <p className="mb-1 text-xs text-ink-3">Submission evidence</p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs text-ink-2">
            {JSON.stringify(s.submission_evidence, null, 2)}
          </pre>
        </div>
      )}

      {s.screenshots.length > 0 && (
        <div>
          <p className="mb-2 text-xs text-ink-3">Screenshots (password and one-time-code fields are masked)</p>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {s.screenshots.map((shot) => (
              <a
                key={shot.index}
                href={screenshotUrl(app.id, s.id, shot.index)}
                target="_blank"
                rel="noreferrer"
                className="group rounded-lg border border-border p-1.5 hover:bg-surface-2"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={screenshotUrl(app.id, s.id, shot.index)}
                  alt={`Screenshot: ${humanize(shot.label)}`}
                  className="h-28 w-full rounded object-cover object-top"
                  loading="lazy"
                />
                <span className="mt-1 block text-[11px] text-ink-2">{humanize(shot.label)}</span>
              </a>
            ))}
          </div>
        </div>
      )}

      <LiveSubmit app={app} session={s} />
      <SessionActions app={app} session={s} />
    </div>
  );
}

export default async function ApplicationPage(props: PageProps<"/applications/[id]">) {
  const { id } = await props.params;
  const res = await getApplication(id);
  if (!res.ok) {
    if (res.error.includes("404") || res.error.includes("422")) notFound();
    return (
      <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
        {res.error}
      </div>
    );
  }
  const app = res.data;
  const current = app.sessions[0] ?? null;
  const older = app.sessions.slice(1);

  return (
    <div className="flex flex-col gap-6">
      <Link href="/applications" className="inline-flex items-center gap-1 text-sm text-ink-2 hover:text-ink">
        <ArrowLeft aria-hidden className="size-4" /> All applications
      </Link>
      <ExecutionModeBar mode={app} />
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm text-ink-3">{app.company ?? "Unknown company"}</p>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">{app.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={app.job_status} />
            <StatusBadge status={app.application_status} />
            <Link href={`/jobs/${app.job_id}`} className="text-sm text-accent hover:underline">
              Job details
            </Link>
          </div>
          {app.user_action_required && (
            <p className="mt-2 flex items-start gap-1.5 text-sm text-ink">
              <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
              {app.user_action_required}
            </p>
          )}
        </div>
        <StartExecution app={app} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-6 lg:col-span-2">
          <Card title="Application execution" description={current ? `Session started ${relativeTime(current.created_at)}` : undefined}>
            {current ? (
              <Session app={app} s={current} />
            ) : (
              <div className="flex flex-col gap-3">
                <ExecutionBanner mode="DRY_RUN" />
                <p className="text-sm text-ink-2">
                  No browser session yet. A dry run needs the job to be READY_TO_APPLY (draft, validated tailored resume
                  and every standard answer).
                </p>
              </div>
            )}
          </Card>
          {older.length > 0 && (
            <Card title="Earlier sessions">
              <ul className="divide-y divide-border text-sm">
                {older.map((s) => (
                  <li key={s.id} className="flex flex-wrap items-center gap-2 py-2">
                    <StatusBadge status={s.mode} label={s.mode === "LIVE" ? "LIVE" : "DRY RUN"} />
                    <StatusBadge status={s.state} />
                    <span className="text-ink-2">{s.current_step}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>

        <div className="flex flex-col gap-6">
          <ResumeUsedCard resume={app.resume} />
          {app.last_error && (
            <Card title="Last error">
              <p className="text-sm text-critical">{app.last_error}</p>
            </Card>
          )}
          <Card title="Readiness">
            <StatusBadge status={app.readiness.ready ? "READY_TO_APPLY" : app.job_status} />
            {[...app.readiness.user_blockers, ...app.readiness.pending_steps].length > 0 && (
              <ul className="mt-2 list-disc space-y-0.5 pl-4 text-xs text-ink-2">
                {[...app.readiness.user_blockers, ...app.readiness.pending_steps].map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="External actions" description="Every external action, including refusals">
            <ul className="space-y-2 text-xs">
              {app.external_actions.slice(0, 30).map((a, i) => (
                <li key={`${a.at}-${i}`}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <StatusBadge status={a.result} />
                    <span className="font-medium text-ink">{a.tool}</span>
                    <span className="text-ink-3">{a.mode === "LIVE" ? "LIVE" : "dry run"} · {a.domain ?? "—"}</span>
                  </div>
                  <p className="text-ink-2">{a.summary}</p>
                  <time className="text-ink-3" suppressHydrationWarning>{relativeTime(a.at)}</time>
                </li>
              ))}
              {app.external_actions.length === 0 && <li className="text-ink-3">None yet.</li>}
            </ul>
          </Card>
          <Card title="Timeline">
            <ol className="space-y-2 text-xs">
              {app.timeline.slice(0, 30).map((e, i) => (
                <li key={`${e.at}-${i}`}>
                  <p className="text-ink">{humanize(e.event_type)}</p>
                  {e.reason && <p className="text-ink-2">{e.reason}</p>}
                  <time className="text-ink-3" suppressHydrationWarning>{relativeTime(e.at)}</time>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
    </div>
  );
}
