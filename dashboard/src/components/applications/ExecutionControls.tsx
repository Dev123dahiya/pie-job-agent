"use client";

import clsx from "clsx";
import { Loader2, OctagonAlert, Play, Send, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { canSubmit, failureText } from "@/lib/execution";
import type { ApplicationDetail, BrowserSessionView, MissingAnswer } from "@/lib/types";

type Result = { ok: boolean; data: Record<string, unknown> };

async function post(path: string, body?: unknown): Promise<Result> {
  const res = await fetch(`/backend/api/applications/${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    // non-JSON error body
  }
  return { ok: res.ok, data };
}

const message = (r: Result, fallback: string) =>
  typeof r.data.detail === "string" ? r.data.detail : fallback;

function Button({ onClick, busy, children, tone = "secondary", disabled, type = "button" }: {
  onClick?: () => void;
  busy?: boolean;
  children: React.ReactNode;
  tone?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={busy || disabled}
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-60",
        tone === "primary" && "bg-ink text-bg hover:opacity-90",
        tone === "secondary" && "border border-border bg-surface text-ink hover:bg-surface-2",
        tone === "danger" && "bg-critical text-white hover:opacity-90",
      )}
    >
      {busy && <Loader2 aria-hidden className="size-4 animate-spin" />}
      {children}
    </button>
  );
}

function useRun() {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const run = async (key: string, fn: () => Promise<string>) => {
    setBusy(key);
    setNote(null);
    try {
      setNote({ text: await fn(), error: false });
      router.refresh();
    } catch (e) {
      setNote({ text: (e as Error).message, error: true });
    } finally {
      setBusy(null);
    }
  };
  return { busy, note, run };
}

/** Start a dry run (always available) or prepare a live submission (only if enabled). */
export function StartExecution({ app }: { app: ApplicationDetail }) {
  const { busy, note, run } = useRun();
  const describe = (r: Result) => {
    if (!r.ok) throw new Error(message(r, "Execution failed"));
    if (r.data.blocked_needs_user) return `Paused for you: ${String(r.data.user_action ?? "")}`;
    if (r.data.dry_run_complete) return "Dry run complete. Nothing has been submitted.";
    if (r.data.failed) throw new Error(failureText(r.data));
    if (r.data.error) return `Stopped: ${String(r.data.error)}`;
    return `Session is ${String(r.data.state ?? "").toLowerCase().replace(/_/g, " ")}`;
  };
  return (
    <div className="flex flex-col items-start gap-2">
      <div className="flex flex-wrap gap-2">
        <Button busy={busy === "dry"} tone="primary" onClick={() => run("dry", async () => describe(await post(`${app.id}/dry-run`)))}>
          <Play aria-hidden className="size-4" /> Run DRY RUN
        </Button>
        {app.external_live_enabled && (
          <Button busy={busy === "live"} tone="danger" onClick={() => run("live", async () => describe(await post(`${app.id}/live/prepare`)))}>
            <OctagonAlert aria-hidden className="size-4" /> Prepare LIVE submission
          </Button>
        )}
      </div>
      <p className="text-[11px] text-ink-3">
        {app.external_live_enabled
          ? "Preparing a live submission fills the real form but never submits without your confirmation."
          : "Live submission is disabled (EXTERNAL_LIVE_ENABLED is off). Dry runs never submit."}
      </p>
      {note && <p role="status" className={clsx("text-xs", note.error ? "text-critical" : "text-ink-2")}>{note.text}</p>}
    </div>
  );
}

/** Answer the questions PIE could not answer from your verified profile. */
export function AnswerQuestions({ appId, missing }: { appId: string; missing: MissingAnswer[] }) {
  const { busy, note, run } = useRun();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [remember, setRemember] = useState(false);
  const answered = missing.filter((m) => (answers[m.question_key] ?? "").trim());
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        void run("answers", async () => {
          const r = await post(`${appId}/answers`, {
            answers: answered.map((m) => ({
              question_key: m.question_key, question: m.question, answer: answers[m.question_key], remember,
            })),
          });
          if (!r.ok) throw new Error(message(r, "Could not save the answers"));
          return "Saved as verified answers. Run the dry run again to continue.";
        });
      }}
    >
      {missing.map((m) => (
        <label key={m.question_key} className="flex flex-col gap-1">
          <span className="text-sm text-ink">{m.question}</span>
          <span className="text-[11px] text-ink-3">{m.reason}</span>
          <input
            value={answers[m.question_key] ?? ""}
            onChange={(e) => setAnswers({ ...answers, [m.question_key]: e.target.value })}
            className="rounded-lg border border-border bg-surface px-2 py-1.5 text-sm text-ink"
            placeholder="Your answer (entered exactly as written)"
          />
        </label>
      ))}
      <label className="flex items-center gap-2 text-xs text-ink-2">
        <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
        Reuse these answers for other applications
      </label>
      <div>
        <Button type="submit" busy={busy === "answers"} disabled={answered.length === 0}>
          Save answers
        </Button>
      </div>
      {note && <p role="status" className={clsx("text-xs", note.error ? "text-critical" : "text-ink-2")}>{note.text}</p>}
    </form>
  );
}

/** In-page confirmation (never a browser dialog). Submitting needs an explicit tick. */
export function LiveSubmit({ app, session }: { app: ApplicationDetail; session: BrowserSessionView }) {
  const { busy, note, run } = useRun();
  const [open, setOpen] = useState(false);
  const [ticked, setTicked] = useState(false);
  if (!canSubmit(session, app.external_live_enabled)) return null;
  const base = `${app.id}/sessions/${session.id}`;
  return (
    <div className="flex flex-col gap-2">
      {!open ? (
        <Button tone="danger" onClick={() => setOpen(true)}>
          <Send aria-hidden className="size-4" /> Submit application…
        </Button>
      ) : (
        <div role="alertdialog" aria-labelledby="confirm-title" className="rounded-xl border-2 border-critical bg-critical/5 p-4">
          <p id="confirm-title" className="text-sm font-semibold text-critical">Confirm LIVE submission</p>
          <p className="mt-2 text-sm text-ink">{app.confirmation_statement}</p>
          <label className="mt-3 flex items-start gap-2 text-sm text-ink">
            <input type="checkbox" checked={ticked} onChange={(e) => setTicked(e.target.checked)} className="mt-1" />
            I have reviewed the filled form and I want PIE to submit it now.
          </label>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              tone="danger"
              busy={busy === "submit"}
              disabled={!ticked}
              onClick={() =>
                run("submit", async () => {
                  const c = await post(`${base}/confirm-live`, { acknowledged_statement: app.confirmation_statement });
                  if (!c.ok) throw new Error(message(c, "Confirmation failed"));
                  const r = await post(`${base}/submit`);
                  if (!r.ok) throw new Error(message(r, "Submission refused"));
                  setOpen(false);
                  return r.data.submitted
                    ? "Submitted - the employer confirmed it."
                    : `Not confirmed (${String(r.data.outcome)}). Nothing will be retried; check your email.`;
                })
              }
            >
              Submit now
            </Button>
            <Button onClick={() => { setOpen(false); setTicked(false); }}>
              <X aria-hidden className="size-4" /> Cancel
            </Button>
          </div>
        </div>
      )}
      {note && <p role="status" className={clsx("text-xs", note.error ? "text-critical" : "text-ink-2")}>{note.text}</p>}
    </div>
  );
}

/** Close an open session, or settle an unconfirmed live submission after checking. */
export function SessionActions({ app, session }: { app: ApplicationDetail; session: BrowserSessionView }) {
  const { busy, note, run } = useRun();
  const base = `${app.id}/sessions/${session.id}`;
  const open = !["CLOSED", "SUBMITTED", "FAILED"].includes(session.state);
  const unresolved = session.mode === "LIVE" && app.submission_attempts.some((a) =>
    ["PENDING", "AMBIGUOUS", "FAILED"].includes(a.outcome));
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {open && !unresolved && (
          <Button busy={busy === "close"} onClick={() => run("close", async () => {
            const r = await post(`${base}/close`);
            if (!r.ok) throw new Error(message(r, "Could not close"));
            return "Session closed. Nothing was submitted.";
          })}>Close session</Button>
        )}
        {unresolved && (
          <>
            <Button busy={busy === "yes"} onClick={() => run("yes", async () => {
              const r = await post(`${base}/resolve`, { submitted: true, note: "Confirmed by the user in the dashboard" });
              if (!r.ok) throw new Error(message(r, "Could not resolve"));
              return "Recorded as submitted (your confirmation).";
            })}>I confirmed it was submitted</Button>
            <Button busy={busy === "no"} onClick={() => run("no", async () => {
              const r = await post(`${base}/resolve`, { submitted: false, note: "The user found it was not submitted" });
              if (!r.ok) throw new Error(message(r, "Could not resolve"));
              return "Recorded as not submitted.";
            })}>It was not submitted</Button>
          </>
        )}
      </div>
      {note && <p role="status" className={clsx("text-xs", note.error ? "text-critical" : "text-ink-2")}>{note.text}</p>}
    </div>
  );
}
