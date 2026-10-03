"use client";

import { ExternalLink, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { failedScreenshotUrl, type FailedJob } from "@/lib/failed";
import { humanize, relativeTime } from "@/lib/format";

type Result = { job_id: string; ok: boolean; status?: string; error?: string };

/** Failed jobs. Retrying is always the user's choice (REQUIREMENTS 12): one or several. */
export function FailedList({ items }: { items: FailedJob[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };

  async function retry(ids: string[]) {
    if (ids.length === 0) return;
    if (ids.length > 1 && !window.confirm(`Put ${ids.length} failed jobs back in the queue? Nothing is submitted.`)) return;
    setBusy(true);
    setMessage(null);
    try {
      const res = await fetch("/backend/api/jobs/retry-batch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ job_ids: ids }),
      });
      const data = (await res.json().catch(() => ({}))) as { results?: Result[]; retried?: number; detail?: unknown };
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Retry failed");
      const errors = (data.results ?? []).filter((r) => !r.ok);
      setMessage({
        text:
          `${data.retried ?? 0} job(s) back in the queue.` +
          (errors.length ? ` Not retried: ${errors.map((e) => e.error).join("; ")}` : ""),
        error: errors.length > 0,
      });
      setSelected(new Set());
      router.refresh();
    } catch (err) {
      setMessage({ text: (err as Error).message, error: true });
    } finally {
      setBusy(false);
    }
  }

  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-ink-2">
        No failed jobs.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={selected.size === items.length}
            onChange={() => setSelected(selected.size === items.length ? new Set() : new Set(items.map((i) => i.job_id)))}
          />
          Select all
        </label>
        <button
          type="button"
          disabled={busy || selected.size === 0}
          onClick={() => retry([...selected])}
          className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-bg hover:opacity-90 disabled:opacity-50"
        >
          <RotateCcw aria-hidden className="size-4" /> Retry selected ({selected.size})
        </button>
        <span className="text-xs text-ink-3">A retry only puts the job back in the queue. Nothing is opened or sent.</span>
      </div>
      {message && (
        <p role="status" className={message.error ? "text-sm text-critical" : "text-sm text-ink-2"}>
          {message.text}
        </p>
      )}
      <ul className="flex flex-col gap-3">
        {items.map((f) => {
          const shot = failedScreenshotUrl(f);
          return (
            <li key={f.job_id} className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-4 sm:flex-row">
              <input
                type="checkbox"
                aria-label={`Select ${f.title}`}
                checked={selected.has(f.job_id)}
                onChange={() => toggle(f.job_id)}
                className="mt-1 self-start"
              />
              <div className="min-w-0 flex-1">
                <p className="text-xs text-ink-3">{f.company ?? "Unknown company"}</p>
                <Link href={`/jobs/${f.job_id}`} className="font-medium text-ink hover:underline">
                  {f.title}
                </Link>
                <p className="mt-1 text-xs text-ink-3" suppressHydrationWarning>
                  Failed at {humanize(f.stage ?? "unknown step")}
                  {f.failed_at ? ` · ${relativeTime(f.failed_at)}` : ""}
                  {f.retries > 0 ? ` · retried ${f.retries}×` : ""}
                </p>
                {f.error && <p className="mt-1 break-words text-sm text-critical">{f.error}</p>}
                <div className="mt-2 flex flex-wrap gap-3 text-xs">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => retry([f.job_id])}
                    className="inline-flex items-center gap-1 text-accent hover:underline disabled:opacity-50"
                  >
                    <RotateCcw aria-hidden className="size-3" /> Retry
                  </button>
                  {f.application_id && (
                    <Link href={`/applications/${f.application_id}`} className="text-accent hover:underline">
                      Application
                    </Link>
                  )}
                  <a href={f.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                    Posting <ExternalLink aria-hidden className="size-3" />
                  </a>
                </div>
              </div>
              {shot && (
                <a href={shot} target="_blank" rel="noreferrer" className="shrink-0 rounded-lg border border-border p-1 hover:bg-surface-2">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={shot} alt={`Screenshot when ${f.title} failed`} className="h-28 w-44 rounded object-cover object-top" loading="lazy" />
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
