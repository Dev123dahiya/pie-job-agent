import { ExternalLink } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { AnswerForm } from "@/components/needs-input/AnswerForm";
import { SavedAnswers } from "@/components/needs-input/SavedAnswers";
import { Card } from "@/components/ui/Card";
import { getNeedsInput } from "@/lib/api";
import { relativeTime } from "@/lib/format";

export const metadata: Metadata = { title: "Needs my input" };
export const dynamic = "force-dynamic";

export default async function NeedsInputPage() {
  const res = await getNeedsInput();
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Needs my input</h1>
        <p className="mt-1 text-sm text-ink-2">
          Jobs PIE paused because only you can answer. It never guesses: answer each question once and it is reused
          for every application.
        </p>
      </div>

      {!res.ok ? (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
          {res.error}
        </div>
      ) : (
        <>
          {res.data.jobs.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-ink-2">
              Nothing is waiting for you.
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              {res.data.jobs.map((job) => (
                <Card
                  key={job.job_id}
                  title={job.title}
                  description={`${job.company ?? "Unknown company"}${job.paused_at ? ` · paused ${relativeTime(job.paused_at)}` : ""}`}
                  action={
                    <div className="flex items-center gap-3 text-xs">
                      {job.application_id && (
                        <Link href={`/applications/${job.application_id}`} className="text-accent hover:underline">
                          Application
                        </Link>
                      )}
                      <a href={job.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                        Posting <ExternalLink aria-hidden className="size-3" />
                      </a>
                    </div>
                  }
                >
                  {job.reason && <p className="mb-3 whitespace-pre-line text-xs text-ink-2">{job.reason}</p>}
                  {job.questions.length === 0 ? (
                    <p className="text-sm text-ink-2">
                      All questions are answered.{" "}
                      {job.in_browser ? "Resume the application to continue." : "It goes back in the queue on the next check."}
                    </p>
                  ) : (
                    <AnswerForm job={job} />
                  )}
                </Card>
              ))}
            </div>
          )}

          <Card title="Saved answers" description="Reused for every application. Edit or delete them here.">
            <SavedAnswers answers={res.data.saved_answers} />
          </Card>
        </>
      )}
    </div>
  );
}
