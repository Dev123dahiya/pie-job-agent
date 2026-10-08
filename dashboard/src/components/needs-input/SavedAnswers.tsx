"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { SavedAnswer } from "@/lib/needsInput";

/** Your reusable answers: check, edit or delete them. Shown only on this computer. */
export function SavedAnswers({ answers }: { answers: SavedAnswer[] }) {
  const router = useRouter();
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function call(method: "PUT" | "DELETE", id: string, body?: unknown) {
    setError(null);
    const res = await fetch(`/backend/api/needs-input/saved-answers/${encodeURIComponent(id)}`, {
      method,
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
      const data = (await res.json().catch(() => ({}))) as { detail?: unknown };
      setError(typeof data.detail === "string" ? data.detail : "That did not work");
      return;
    }
    setEditing(null);
    router.refresh();
  }

  if (answers.length === 0) {
    return <p className="text-sm text-ink-2">No saved answers yet. Answers you give above appear here.</p>;
  }
  return (
    <div className="flex flex-col gap-2">
      {error && (
        <p role="alert" className="text-xs text-critical">
          {error}
        </p>
      )}
      <ul className="divide-y divide-border">
        {answers.map((a) => (
          <li key={a.id} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0">
            <p className="text-sm text-ink">{a.question}</p>
            <p className="font-mono text-[11px] text-ink-3">{a.question_key}</p>
            {editing === a.id ? (
              <div className="flex flex-wrap items-center gap-2">
                <input
                  aria-label={`New answer for ${a.question}`}
                  value={draft}
                  maxLength={2000}
                  onChange={(e) => setDraft(e.target.value)}
                  className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-sm text-ink"
                />
                <button
                  type="button"
                  disabled={!draft.trim()}
                  onClick={() => call("PUT", a.id, { answer: draft.trim() })}
                  className="rounded-lg bg-ink px-3 py-1.5 text-sm text-bg disabled:opacity-50"
                >
                  Save
                </button>
                <button type="button" onClick={() => setEditing(null)} className="px-2 py-1.5 text-sm text-ink-2">
                  Cancel
                </button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-3">
                <p className="text-sm font-medium text-ink">{a.answer}</p>
                <button
                  type="button"
                  onClick={() => {
                    setEditing(a.id);
                    setDraft(a.answer);
                  }}
                  className="text-xs text-accent hover:underline"
                >
                  Edit
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (window.confirm(`Delete your saved answer to "${a.question}"?`)) void call("DELETE", a.id);
                  }}
                  className="text-xs text-critical hover:underline"
                >
                  Delete
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
