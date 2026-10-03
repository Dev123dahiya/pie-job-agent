"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { KIND_LABEL, answersToSend, type PausedJob } from "@/lib/needsInput";

const FIELD =
  "w-full rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

/** Answer a paused job's questions once; the answers are reused for every application. */
export function AnswerForm({ job }: { job: PausedJob }) {
  const router = useRouter();
  const [values, setValues] = useState<Record<string, string>>({});
  const [state, setState] = useState<{ busy: boolean; message: string | null; error: boolean }>({
    busy: false,
    message: null,
    error: false,
  });
  const toSend = answersToSend(job.questions, values);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (toSend.length === 0) return;
    setState({ busy: true, message: null, error: false });
    try {
      const res = await fetch("/backend/api/needs-input/answers", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ answers: toSend }),
      });
      const data = (await res.json().catch(() => ({}))) as { detail?: string; requeued?: string[] };
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not save the answers");
      const back = data.requeued?.includes(job.job_id);
      setState({
        busy: false,
        error: false,
        message: back
          ? job.in_browser
            ? "Saved. Resume the application to continue."
            : "Saved. The job is back in the queue."
          : "Saved.",
      });
      setValues({});
      router.refresh();
    } catch (err) {
      setState({ busy: false, message: (err as Error).message, error: true });
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      {job.questions.map((q) => (
        <div key={q.question_key} className="flex flex-col gap-1">
          <label htmlFor={`${job.job_id}-${q.question_key}`} className="text-sm font-medium text-ink">
            {q.question}
          </label>
          <p className="text-xs text-ink-3">
            {KIND_LABEL[q.kind]}
            {q.reason ? ` · ${q.reason}` : ""}
          </p>
          {q.answerable ? (
            <textarea
              id={`${job.job_id}-${q.question_key}`}
              rows={1}
              maxLength={2000}
              value={values[q.question_key] ?? ""}
              onChange={(e) => setValues({ ...values, [q.question_key]: e.target.value })}
              className={FIELD}
              placeholder="Your answer (true facts only)"
            />
          ) : null}
        </div>
      ))}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={state.busy || toSend.length === 0}
          className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-bg hover:opacity-90 disabled:opacity-50"
        >
          {state.busy ? "Saving…" : "Save answers"}
        </button>
        <p className="text-xs text-ink-3">Saved once, reused for every application.</p>
      </div>
      {state.message && (
        <p role="status" className={state.error ? "text-xs text-critical" : "text-xs text-ink-2"}>
          {state.message}
        </p>
      )}
    </form>
  );
}
