"use client";

import { Square } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { ACTIVE_RUN_STATUSES, type PipelineRunDetail } from "@/lib/runs";

const POLL_MS = 3000;

/** Live progress of one run: refreshes every few seconds while it runs; Stop is safe
 * (it stops after the current job). */
export function RunProgress({ initial }: { initial: PipelineRunDetail }) {
  const router = useRouter();
  const [run, setRun] = useState(initial);
  const [stopping, setStopping] = useState(false);
  const active = ACTIVE_RUN_STATUSES.has(run.status);

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(async () => {
      try {
        const res = await fetch(`/backend/api/runs/${encodeURIComponent(run.id)}`, { cache: "no-store" });
        if (!res.ok) return;
        const next = (await res.json()) as PipelineRunDetail;
        setRun(next);
        if (!ACTIVE_RUN_STATUSES.has(next.status)) router.refresh();
      } catch {
        // offline for a moment: the next poll retries
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [active, run.id, router]);

  async function stop() {
    setStopping(true);
    try {
      const res = await fetch(`/backend/api/runs/${encodeURIComponent(run.id)}/stop`, { method: "POST" });
      if (res.ok) setRun({ ...run, ...((await res.json()) as Partial<PipelineRunDetail>) });
    } finally {
      setStopping(false);
    }
  }

  const counts = run.report?.counts ?? (run.progress as Record<string, number> | null) ?? {};
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={run.status} />
        <span className="text-sm text-ink">{run.task}</span>
        {active && (
          <button
            type="button"
            onClick={stop}
            disabled={stopping || run.status === "STOPPING"}
            className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-critical px-3 py-1.5 text-sm text-critical hover:bg-critical/10 disabled:opacity-50"
          >
            <Square aria-hidden className="size-3.5" /> {run.status === "STOPPING" ? "Stopping after this job…" : "Stop"}
          </button>
        )}
      </div>
      {active && (
        <div>
          <div className="mb-1 flex justify-between text-xs text-ink-2">
            <span>{run.current_task ?? "Working…"}</span>
            <span className="tabular">
              step {Math.min(run.steps_done + 1, run.steps_total)} of {run.steps_total}
            </span>
          </div>
          <div
            role="progressbar"
            aria-label="Run progress"
            aria-valuemin={0}
            aria-valuemax={run.steps_total}
            aria-valuenow={run.steps_done}
            className="h-2 overflow-hidden rounded-full bg-surface-2"
          >
            <div
              className="h-full rounded-full bg-accent"
              style={{ width: `${run.steps_total ? (run.steps_done / run.steps_total) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}
      {Object.keys(counts).filter((k) => k !== "step").length > 0 && (
        <dl className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
          {Object.entries(counts)
            .filter(([k, v]) => k !== "step" && typeof v === "number")
            .map(([k, v]) => (
              <div key={k} className="rounded-lg bg-surface-2 px-3 py-2">
                <dt className="text-xs text-ink-3">{k.replace(/_/g, " ")}</dt>
                <dd className="tabular font-semibold text-ink">{v as number}</dd>
              </div>
            ))}
        </dl>
      )}
      {run.summary && !active && <p className="text-sm text-ink-2">{run.summary}</p>}
      {(run.report?.failures?.length ?? 0) > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-critical">
          {run.report!.failures!.slice(0, 10).map((f, i) => (
            <li key={i}>
              {f.step}: {f.error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
