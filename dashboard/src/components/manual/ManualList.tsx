"use client";

import { Check, ExternalLink, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { relativeTime } from "@/lib/format";

export type ManualJob = {
  job_id: string;
  title: string;
  company: string | null;
  url: string;
  match_score: number | null;
  reason: string | null;
  added_at: string | null;
};

/** Jobs you apply to yourself: open the link, then mark them applied (or skip). */
export function ManualList({ items }: { items: ManualJob[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(jobId: string, action: "applied" | "skip") {
    setBusy(jobId);
    setError(null);
    try {
      const res = await fetch(`/backend/api/jobs/${encodeURIComponent(jobId)}/manual/${action}`, { method: "POST" });
      const data = (await res.json().catch(() => ({}))) as { detail?: unknown };
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "That did not work");
      router.refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-sm text-ink-2">
        Nothing to apply to by hand.
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {error && (
        <p role="alert" className="text-sm text-critical">
          {error}
        </p>
      )}
      <div className="overflow-x-auto rounded-xl border border-border bg-surface">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="border-b border-border text-xs text-ink-3">
            <tr>
              <th className="px-3 py-2 font-medium">Company · Role</th>
              <th className="px-3 py-2 font-medium">Match</th>
              <th className="px-3 py-2 font-medium">Why manual</th>
              <th className="px-3 py-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((m) => (
              <tr key={m.job_id} className="align-top">
                <td className="px-3 py-2.5">
                  <p className="text-xs text-ink-3">{m.company ?? "Unknown company"}</p>
                  <Link href={`/jobs/${m.job_id}`} className="font-medium text-ink hover:underline">
                    {m.title}
                  </Link>
                  {m.added_at && (
                    <p className="text-xs text-ink-3" suppressHydrationWarning>
                      added {relativeTime(m.added_at)}
                    </p>
                  )}
                </td>
                <td className="tabular px-3 py-2.5 text-ink">{m.match_score ?? "—"}</td>
                <td className="max-w-[240px] px-3 py-2.5 text-xs text-ink-2">{m.reason ?? "—"}</td>
                <td className="px-3 py-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <a
                      href={m.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 rounded-lg bg-ink px-2.5 py-1.5 text-xs font-medium text-bg hover:opacity-90"
                    >
                      Apply <ExternalLink aria-hidden className="size-3" />
                    </a>
                    <button
                      type="button"
                      disabled={busy === m.job_id}
                      onClick={() => act(m.job_id, "applied")}
                      className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs text-ink hover:bg-surface-2 disabled:opacity-50"
                    >
                      <Check aria-hidden className="size-3" /> Mark as applied
                    </button>
                    <button
                      type="button"
                      disabled={busy === m.job_id}
                      onClick={() => act(m.job_id, "skip")}
                      className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs text-ink-2 hover:bg-surface-2 disabled:opacity-50"
                    >
                      <X aria-hidden className="size-3" /> Skip
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
