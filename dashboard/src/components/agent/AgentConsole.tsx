"use client";

import clsx from "clsx";
import { Loader2, Play, RotateCcw, Send, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ExecutionBanner } from "@/components/applications/ExecutionBanner";
import { ExecutionModeBar } from "@/components/applications/ExecutionModeBar";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { describeAction, TASK_PRESETS, visibleActions } from "@/lib/agentEvents";
import { availableModes } from "@/lib/execution";
import { humanize, relativeTime } from "@/lib/format";
import type { AgentStatus, ExecutionModeName, RunDetail, RunInfo, ToolInfo } from "@/lib/types";

const LIVE = new Set(["RUNNING", "STOPPING", "PENDING"]);
const FIELD =
  "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/backend/api/${path}`, {
    cache: "no-store",
    headers: { "content-type": "application/json" },
    ...init,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`);
  return body as T;
}

function elapsed(run: RunInfo): string {
  const end = run.ended_at ? new Date(run.ended_at) : new Date();
  const s = Math.max(0, Math.round((end.getTime() - new Date(run.started_at).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function CurrentRun({ run, onStop, onResume, busy }: {
  run: RunDetail;
  onStop: () => void;
  onResume: (message: string | null, extra: number) => void;
  busy: boolean;
}) {
  const [reply, setReply] = useState("");
  const total = run.max_iterations ?? 0;
  const pct = total ? Math.min(100, (run.iterations / total) * 100) : 0;
  const limitHit = run.status === "FAILED" && (run.summary ?? "").startsWith("Iteration limit");
  return (
    <Card
      title="Current run"
      description={`${humanize(run.mode ?? "")} · ${run.model ?? "Claude"} · started ${relativeTime(run.started_at)}`}
      action={<StatusBadge status={run.status} />}
    >
      <div className="flex flex-col gap-4">
        <div>
          <p className="text-xs text-ink-3">Task</p>
          <p className="text-sm text-ink">{run.task}</p>
        </div>
        {LIVE.has(run.status) && (
          <div>
            <p className="text-xs text-ink-3">Current action</p>
            <p className="flex items-center gap-2 text-sm text-ink">
              <Loader2 aria-hidden className="size-3.5 animate-spin text-accent" />
              {run.current_task ?? "Working"}
            </p>
          </div>
        )}
        <div>
          <div className="mb-1 flex justify-between text-xs text-ink-2">
            <span>Steps</span>
            <span className="tabular" suppressHydrationWarning>
              {run.iterations}/{total} · {elapsed(run)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-surface-2" role="progressbar" aria-label="Steps"
            aria-valuemin={0} aria-valuemax={total} aria-valuenow={run.iterations}>
            <div className="h-full rounded-full bg-series-1 transition-[width] duration-500" style={{ width: `${pct}%` }} />
          </div>
        </div>

        {run.status === "NEEDS_USER_INPUT" && (
          <div className="rounded-lg border border-warning/60 bg-warning/10 p-3" role="alert">
            <p className="text-xs font-semibold uppercase tracking-wider text-ink">Needs your input</p>
            <p className="mt-1 whitespace-pre-line text-sm text-ink">{run.pending_user_input}</p>
            <textarea
              value={reply}
              onChange={(e) => setReply(e.target.value)}
              rows={3}
              maxLength={4000}
              placeholder="Your answer (only facts you want the agent to use)"
              className={clsx(FIELD, "mt-2")}
            />
            <button type="button" disabled={busy || !reply.trim()} onClick={() => onResume(reply.trim(), 0)}
              className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-bg disabled:opacity-50">
              <Send aria-hidden className="size-3.5" /> Send and resume
            </button>
          </div>
        )}

        {run.summary && !LIVE.has(run.status) && run.status !== "NEEDS_USER_INPUT" && (
          <div>
            <p className="text-xs text-ink-3">Result</p>
            <p className="whitespace-pre-line text-sm text-ink">{run.summary}</p>
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {(LIVE.has(run.status) || run.status === "NEEDS_USER_INPUT") && (
            <button type="button" onClick={onStop} disabled={busy || run.status === "STOPPING"}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-ink-2 hover:bg-surface-2 disabled:opacity-60">
              <Square aria-hidden className="size-3.5" />
              {run.status === "STOPPING" ? "Stopping after this step…" : "Stop"}
            </button>
          )}
          {limitHit && (
            <button type="button" onClick={() => onResume(null, 5)} disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm text-ink-2 hover:bg-surface-2">
              <RotateCcw aria-hidden className="size-3.5" /> Resume with 5 more steps
            </button>
          )}
        </div>
      </div>
    </Card>
  );
}

function Timeline({ run }: { run: RunDetail }) {
  const actions = visibleActions(run.actions);
  const toolCalls = run.actions.filter((a) => a.action === "TOOL_CALL_COMPLETED").length;
  const failures = run.actions.filter((a) => a.status === "FAILED").length;
  return (
    <Card title="Activity" description={`${toolCalls} tool calls · ${failures} failures · summaries only, no hidden reasoning`}>
      <ol className="flex flex-col gap-2" aria-label="Run events">
        {actions.map((a) => {
          const v = describeAction(a);
          return (
            <li key={a.sequence}
              className={clsx("rounded-lg border px-3 py-2", v.failed ? "border-critical/50" : "border-border",
                v.kind === "internal" && "opacity-70")}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="flex min-w-0 items-center gap-2 text-sm text-ink">
                  {a.iteration != null && <span className="tabular text-xs text-ink-3">#{a.iteration}</span>}
                  <span className="truncate">{v.title}</span>
                </p>
                <StatusBadge status={v.status} label={v.kind === "thinking" ? "Step" : undefined} />
              </div>
              {v.detail && <p className="mt-1 whitespace-pre-line text-xs text-ink-2">{v.detail}</p>}
              {v.kind === "tool" && (a.input || a.output) && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-xs text-accent">Input and output</summary>
                  <pre className="mt-1 max-h-64 overflow-auto rounded bg-surface-2 p-2 text-[11px] text-ink-2">
                    {JSON.stringify({ input: a.input?.arguments ?? a.input, output: a.output }, null, 1)}
                  </pre>
                </details>
              )}
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

export function AgentConsole({ initialStatus, initialRun, initialHistory, tools }: {
  initialStatus: AgentStatus | null;
  initialRun: RunDetail | null;
  initialHistory: RunInfo[];
  tools: ToolInfo[];
}) {
  const [status, setStatus] = useState(initialStatus);
  const [run, setRun] = useState(initialRun);
  const [history, setHistory] = useState(initialHistory);
  const [selected, setSelected] = useState<string | null>(initialRun?.id ?? null);
  const [task, setTask] = useState(TASK_PRESETS[0].task);
  const [mode, setMode] = useState<ExecutionModeName>(TASK_PRESETS[0].mode);
  const liveEnabled = status?.external_live_enabled === true;
  const [maxIterations, setMaxIterations] = useState(10);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (id: string | null = selected) => {
    try {
      const [s, h, r] = await Promise.all([
        api<AgentStatus>("agent/status"),
        api<RunInfo[]>("agent/runs?run_type=agent&limit=10"),
        id ? api<RunDetail>(`agent/runs/${id}?actions_limit=300`) : Promise.resolve(null),
      ]);
      setStatus(s);
      setHistory(h);
      setRun(r);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selected]);

  const live = (run && LIVE.has(run.status)) || (status !== null && status.state !== "idle");
  useEffect(() => {
    const id = window.setInterval(() => void refresh(), live ? 2000 : 15000);
    return () => window.clearInterval(id);
  }, [refresh, live]);

  const act = async (fn: () => Promise<RunInfo | null>) => {
    setBusy(true);
    setError(null);
    try {
      const r = await fn();
      if (r) setSelected(r.id);
      await refresh(r?.id ?? selected);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const start = () => act(() => api<RunInfo>("agent/runs", {
    method: "POST", body: JSON.stringify({ task, mode, max_iterations: maxIterations }),
  }));
  const stop = () => act(() => api<RunInfo>(`agent/runs/${run?.id}/cancel`, { method: "POST" }));
  const resume = (message: string | null, extra: number) => act(() => api<RunInfo>(
    `agent/runs/${run?.id}/resume`, { method: "POST", body: JSON.stringify({ message, extra_iterations: extra }) },
  ));

  const groups: [string, ToolInfo[]][] = (["READ_ONLY", "WRITE", "EXTERNAL_ACTION"] as const).map(
    (p) => [p, tools.filter((t) => t.permission === p)],
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">Agent</h1>
          <p className="mt-1 text-sm text-ink-2">
            Claude decides each step; PIE runs only registered, permission-checked tools.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge status={status?.analyzer ?? "unavailable"} label={status?.analyzer_detail ?? "Backend offline"} />

        </div>
      </div>

      <ExecutionModeBar
        mode={
          status
            ? {
                development_mode: status.development_mode,
                external_live_configured: status.external_live_configured,
                external_live_enabled: status.external_live_enabled,
                live_block_reason: null,
                browser_headless: true,
                default_mode: status.default_external_mode,
              }
            : null
        }
      />

      {error && <p role="alert" className="rounded-lg border border-critical/50 bg-surface px-3 py-2 text-sm text-critical">{error}</p>}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-6 lg:col-span-2">
          {run ? (
            <>
              <CurrentRun run={run} onStop={stop} onResume={resume} busy={busy} />
              <Timeline run={run} />
            </>
          ) : (
            <Card title="Current run">
              <p className="text-sm text-ink-2">No agent run yet. Start one on the right.</p>
            </Card>
          )}
        </div>

        <div className="flex flex-col gap-6">
          <Card title="Start a run" description="One run at a time. Dry runs never submit; live needs your confirmation per application.">
            <div className="flex flex-col gap-3">
              <div className="flex flex-wrap gap-1.5">
                {TASK_PRESETS.map((p) => (
                  <button key={p.label} type="button" onClick={() => { setTask(p.task); setMode(p.mode); }}
                    className="rounded-full border border-border px-2.5 py-1 text-xs text-ink-2 hover:bg-surface-2">
                    {p.label}
                  </button>
                ))}
              </div>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-ink-3">Task</span>
                <textarea value={task} onChange={(e) => setTask(e.target.value)} rows={5} maxLength={2000} className={FIELD} />
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-ink-3">Permissions</span>
                  <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)} className={FIELD}>
                    {availableModes(liveEnabled).map((m) => (
                      <option key={m.value} value={m.value}>{m.label}</option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-ink-3">Step limit</span>
                  <input type="number" min={1} max={40} value={maxIterations}
                    onChange={(e) => setMaxIterations(Number(e.target.value))} className={FIELD} />
                </label>
              </div>
              {mode === "external_dry_run" && <ExecutionBanner mode="DRY_RUN" />}
              {mode === "external_live" && <ExecutionBanner mode="LIVE" />}
              <button type="button" onClick={start} disabled={busy || live || task.trim().length < 3}
                className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-ink px-3 py-2 text-sm font-medium text-bg disabled:opacity-50">
                {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : <Play aria-hidden className="size-4" />}
                {live ? "A run is in progress" : "Start agent"}
              </button>
            </div>
          </Card>

          <Card title="Run history">
            {history.length === 0 ? (
              <p className="text-sm text-ink-3">No agent runs yet.</p>
            ) : (
              <ul className="flex flex-col divide-y divide-border">
                {history.map((h) => (
                  <li key={h.id}>
                    <button type="button" onClick={() => { setSelected(h.id); void refresh(h.id); }}
                      className={clsx("w-full py-2 text-left", h.id === run?.id && "font-medium")}>
                      <span className="flex items-center justify-between gap-2">
                        <span className="line-clamp-1 text-sm text-ink">{h.task}</span>
                        <StatusBadge status={h.status} />
                      </span>
                      <span className="text-xs text-ink-3" suppressHydrationWarning>
                        {relativeTime(h.started_at)} · {h.iterations} steps
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Tools" description="What the agent can use">
            <div className="flex flex-col gap-3">
              {groups.map(([perm, list]) => (
                <div key={perm}>
                  <p className="mb-1 text-xs font-medium text-ink-2">{humanize(perm)}</p>
                  <ul className="flex flex-wrap gap-1.5">
                    {list.map((t) => (
                      <li key={t.name} title={t.description}
                        className={clsx("rounded-md border px-2 py-0.5 font-mono text-[11px]",
                          t.enabled ? "border-border text-ink-2" : "border-dashed border-border text-ink-3 line-through")}>
                        {t.name}
                      </li>
                    ))}
                  </ul>
                  {perm === "EXTERNAL_ACTION" && (
                    <p className="mt-1 text-xs text-ink-3">Disabled: not enabled in Phase 3.</p>
                  )}
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
