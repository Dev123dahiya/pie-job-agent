"use client";

import { Check, ExternalLink, Sparkles, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { humanize } from "@/lib/format";
import { PLATFORM_LABEL } from "@/lib/home";
import type { ReviewItem } from "@/lib/runs";

type Outcome = { session_id: string; submitted: boolean; error?: string; code?: string };

/** Applications prepared LIVE and waiting for your yes/no. Approving submits for real. */
export function ReviewQueue({ items }: { items: ReviewItem[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  async function post(path: string, body?: unknown) {
    const res = await fetch(`/backend/api/review/${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "That did not work");
    return data;
  }

  async function run(fn: () => Promise<string>) {
    setBusy(true);
    setMessage(null);
    try {
      setMessage({ text: await fn(), error: false });
      setSelected(new Set());
      router.refresh();
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  }

  const describe = (r: Outcome) => (r.submitted ? "submitted" : `not submitted (${r.code ?? r.error ?? "refused"})`);

  function approve(chosen: ReviewItem[]) {
    if (chosen.length === 0) return;
    const text = chosen.map((i) => `• ${i.confirmation_statement}`).join("\n");
    if (!window.confirm(`Submit ${chosen.length} application(s) for real?\n\n${text}`)) return;
    void run(async () => {
      if (chosen.length === 1) {
        const r = (await post(`${encodeURIComponent(chosen[0].session_id)}/approve`, {
          statement: chosen[0].confirmation_statement,
        })) as Outcome;
        return `${chosen[0].title}: ${describe(r)}`;
      }
      const data = (await post("approve-batch", {
        items: chosen.map((i) => ({ session_id: i.session_id, statement: i.confirmation_statement })),
      })) as { results: Outcome[]; submitted: number };
      return `${data.submitted} of ${chosen.length} submitted.` +
        (data.results.some((r) => !r.submitted) ? ` ${data.results.filter((r) => !r.submitted).map(describe).join("; ")}` : "");
    });
  }

  function reject(item: ReviewItem) {
    if (!window.confirm(`Reject ${item.title}? Nothing is submitted and the job is withdrawn.`)) return;
    void run(async () => {
      await post(`${encodeURIComponent(item.session_id)}/reject`);
      return `${item.title}: rejected, nothing submitted`;
    });
  }

  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-ink-2">
        Nothing is waiting for review. Start a run in review mode from the Runs page.
      </div>
    );
  }
  const chosen = items.filter((i) => selected.has(i.session_id));
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={selected.size === items.length}
            onChange={() =>
              setSelected(selected.size === items.length ? new Set() : new Set(items.map((i) => i.session_id)))
            }
          />
          Select all
        </label>
        <button
          type="button"
          disabled={busy || chosen.length === 0}
          onClick={() => approve(chosen)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-critical px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          <Check aria-hidden className="size-4" /> Approve selected ({chosen.length})
        </button>
      </div>
      {message && (
        <p role="status" className={message.error ? "text-sm text-critical" : "text-sm text-ink-2"}>
          {message.text}
        </p>
      )}
      {items.map((item) => (
        <article key={item.session_id} className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-4">
          <div className="flex flex-wrap items-start gap-3">
            <input
              type="checkbox"
              aria-label={`Select ${item.title}`}
              checked={selected.has(item.session_id)}
              onChange={() => {
                const next = new Set(selected);
                if (next.has(item.session_id)) next.delete(item.session_id);
                else next.add(item.session_id);
                setSelected(next);
              }}
              className="mt-1"
            />
            <div className="min-w-0 flex-1">
              <p className="text-xs text-ink-3">
                {item.company ?? "Unknown company"} · {PLATFORM_LABEL[item.platform]}
              </p>
              <Link href={`/jobs/${item.job_id}`} className="font-medium text-ink hover:underline">
                {item.title}
              </Link>
              <p className="mt-1 text-xs text-ink-2">
                Match {item.match_score ?? "—"}
                {item.match_reason ? ` · ${item.match_reason}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => approve([item])}
                className="inline-flex items-center gap-1 rounded-lg bg-critical px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                <Check aria-hidden className="size-4" /> Approve
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => reject(item)}
                className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1.5 text-sm text-ink hover:bg-surface-2 disabled:opacity-50"
              >
                <X aria-hidden className="size-4" /> Reject
              </button>
              <a
                href={item.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded-lg px-3 py-1.5 text-sm text-accent hover:underline"
              >
                Open job <ExternalLink aria-hidden className="size-3.5" />
              </a>
            </div>
          </div>
          <div className="grid gap-3 text-xs sm:grid-cols-2">
            <div>
              <p className="mb-1 font-medium text-ink-2">Resume that will be sent</p>
              {item.resume ? (
                <Link href={`/resumes/${item.resume.version_id}`} className="text-accent hover:underline">
                  Tailored v{item.resume.version_number}
                </Link>
              ) : (
                <span className="text-ink-3">None</span>
              )}
              {item.uploaded_resume && <p className="break-all text-ink-3">{item.uploaded_resume}</p>}
            </div>
            <div>
              <p className="mb-1 font-medium text-ink-2">Answers that will be filled</p>
              {item.platform === "naukri" && (
                <p className="text-ink-3">
                  Naukri sends your Naukri profile and the resume saved there. If Naukri then asks questions, answer
                  them in the open browser window.
                </p>
              )}
              <ul className="space-y-0.5">
                {item.answers.map((a, i) => (
                  <li key={`${a.key}-${i}`} className="text-ink">
                    {a.label} — <span className="text-ink-3">Filled from: {a.key ?? humanize(a.source ?? "?")}</span>
                    {a.ai_suggested && (
                      <span className="ml-1 inline-flex items-center gap-0.5 rounded bg-warning/15 px-1 text-warning">
                        <Sparkles aria-hidden className="size-3" /> AI-suggested
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <p className="rounded-lg bg-surface-2 px-3 py-2 text-xs text-ink-2">{item.confirmation_statement}</p>
        </article>
      ))}
    </div>
  );
}
