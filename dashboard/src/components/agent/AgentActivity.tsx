"use client";

import clsx from "clsx";
import { Square } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { relativeTime } from "@/lib/format";
import { RUN_STARTED_EVENT } from "@/lib/jobs";
import type { AgentStatus, RunInfo } from "@/lib/types";

const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 15000;

export function runProgress(run: RunInfo): { label: string; done: number; total: number } {
  if (run.run_type === "agent") {
    return { label: "Agent steps", done: run.iterations, total: run.max_iterations ?? 0 };
  }
  if (run.run_type === "analysis") {
    return { label: "Jobs analyzed", done: run.searches_completed, total: run.searches_total };
  }
  return { label: "Searches completed", done: run.searches_completed, total: run.searches_total };
}

export function runMetrics(run: RunInfo): { label: string; value: number }[] {
  if (run.run_type === "agent") {
    return [
      { label: "Steps", value: run.iterations },
      { label: "Step limit", value: run.max_iterations ?? 0 },
      { label: "Errors", value: run.errors.length },
    ];
  }
  if (run.run_type === "analysis") {
    return [
      { label: "Analyzed", value: run.jobs_analyzed },
      { label: "Shortlisted", value: run.jobs_shortlisted },
      { label: "Rejected", value: run.jobs_rejected },
      { label: "Failed", value: run.jobs_failed },
    ];
  }
  return [
    { label: "Jobs discovered", value: run.jobs_found },
    { label: "New jobs", value: run.jobs_new },
    { label: "Duplicates", value: run.jobs_duplicate },
    { label: "Failed", value: run.jobs_failed },
  ];
}

function Metrics({ run }: { run: RunInfo }) {
  return (
    <dl className="grid grid-cols-2 gap-3 @lg:grid-cols-4">
      {runMetrics(run).map((m) => (
        <div key={m.label}>
          <dt className="text-xs text-ink-3">{m.label}</dt>
          <dd className="tabular text-lg font-semibold text-ink">{m.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Progress({ run }: { run: RunInfo }) {
  const { label, done, total } = runProgress(run);
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-ink-2">
        <span>{label}</span>
        <span className="tabular">
          {done}/{total || "?"}
        </span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-surface-2"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
      >
        <div className="h-full rounded-full bg-series-1 transition-[width] duration-500" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function AgentActivity({ initial, className }: { initial: AgentStatus | null; className?: string }) {
  const router = useRouter();
  const [status, setStatus] = useState<AgentStatus | null>(initial);
  const [offline, setOffline] = useState(initial === null);
  const [stopping, setStopping] = useState(false);
  const wasActive = useRef(initial?.state !== "idle" && initial !== null);

  const poll = useCallback(async () => {
    try {
      const res = await fetch("/backend/api/agent/status", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      const next = (await res.json()) as AgentStatus;
      setStatus(next);
      setOffline(false);
      const active = next.state !== "idle";
      if (wasActive.current && !active) router.refresh(); // a run just finished: reload data
      wasActive.current = active;
    } catch {
      setOffline(true);
    }
  }, [router]);

  const active = status !== null && status.state !== "idle";
  useEffect(() => {
    const id = window.setInterval(poll, active ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    window.addEventListener(RUN_STARTED_EVENT, poll);
    return () => {
      window.clearInterval(id);
      window.removeEventListener(RUN_STARTED_EVENT, poll);
    };
  }, [poll, active]);

  const stop = async (runId: string) => {
    setStopping(true);
    try {
      await fetch(`/backend/api/agent/runs/${runId}/cancel`, { method: "POST" });
      await poll();
    } finally {
      setStopping(false);
    }
  };

  const run = status?.active_run ?? null;
  const last = status?.recent_runs[0] ?? null;
  const badge = offline
    ? { status: "unavailable", label: "Offline" }
    : status?.state === "running"
      ? { status: "RUNNING", label: "Running" }
      : status?.state === "stopping"
        ? { status: "STOPPING", label: "Stopping" }
        : { status: "IDLE", label: "Idle" };

  return (
    <section
      className={clsx("@container rounded-xl border border-border bg-surface p-5", className)}
      aria-live="polite"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-ink">PIE Job Agent</h2>
          <p className="mt-0.5 text-xs text-ink-3">
            {status ? `Analyzer: ${status.analyzer_detail}` : "Waiting for the backend"}
          </p>
        </div>
        <StatusBadge status={badge.status} label={badge.label} />
      </header>

      <div className="mt-4 flex flex-col gap-4">
        {run ? (
          <>
            <div>
              <p className="text-xs text-ink-3">Current task</p>
              <p className="text-sm text-ink">{run.current_task ?? run.task}</p>
            </div>
            <Progress run={run} />
            <Metrics run={run} />
            <div>
              <button
                type="button"
                onClick={() => stop(run.id)}
                disabled={stopping || status?.state === "stopping"}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-ink-2 hover:bg-surface-2 disabled:opacity-60"
              >
                <Square aria-hidden className="size-3.5" />
                {status?.state === "stopping" ? "Stopping after this step…" : "Stop"}
              </button>
            </div>
          </>
        ) : last ? (
          <>
            <p className="text-sm text-ink-2">
              Last run: <span className="text-ink">{last.task}</span>
              <span className="text-ink-3" suppressHydrationWarning>
                {" "}
                · {relativeTime(last.ended_at ?? last.started_at)}
              </span>
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={last.status} />
              {last.errors.length > 0 && (
                <span className="text-xs text-ink-3">
                  {last.errors.length} error{last.errors.length === 1 ? "" : "s"}: {last.errors[0].error}
                </span>
              )}
            </div>
            <Metrics run={last} />
          </>
        ) : (
          <p className="text-sm text-ink-2">
            {offline ? "The backend is not reachable." : "No runs yet. Use Discover jobs to start."}
          </p>
        )}
      </div>
    </section>
  );
}
