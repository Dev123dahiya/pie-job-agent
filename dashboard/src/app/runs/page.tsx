import type { Metadata } from "next";
import { RunProgress } from "@/components/runs/RunProgress";
import { StartRunForm } from "@/components/runs/StartRunForm";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { getPipelineRun, getRunOptions, getRuns } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { ACTIVE_RUN_STATUSES } from "@/lib/runs";

export const metadata: Metadata = { title: "Runs" };
export const dynamic = "force-dynamic";

export default async function RunsPage() {
  const [options, runs] = await Promise.all([getRunOptions(), getRuns()]);
  const latest = runs.ok ? runs.data.items[0] : undefined;
  const current = latest ? await getPipelineRun(latest.id) : null;
  const busy = latest ? ACTIVE_RUN_STATUSES.has(latest.status) : false;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Runs</h1>
        <p className="mt-1 text-sm text-ink-2">
          Start the agent: pick the steps, the platforms and the mode. Dry run never submits. Stop is safe: it
          finishes the current job first.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-5">
        <Card title="Start a run" className="lg:col-span-2">
          {options.ok ? (
            <StartRunForm options={options.data} busyRun={busy} />
          ) : (
            <p role="alert" className="text-sm text-ink-2">
              {options.error}
            </p>
          )}
        </Card>
        <Card title={busy ? "Running now" : "Latest run"} className="lg:col-span-3">
          {current?.ok ? (
            <RunProgress key={current.data.id + current.data.status} initial={current.data} />
          ) : (
            <p className="text-sm text-ink-2">No runs yet.</p>
          )}
        </Card>
      </div>

      <Card title="History" description="Runs started here and the daily runs">
        {!runs.ok ? (
          <p role="alert" className="text-sm text-ink-2">
            {runs.error}
          </p>
        ) : runs.data.items.length === 0 ? (
          <p className="text-sm text-ink-2">No runs yet.</p>
        ) : (
          <ul className="divide-y divide-border">
            {runs.data.items.map((r) => (
              <li key={r.id} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status={r.status} />
                  <span className="text-sm text-ink">{r.task}</span>
                  <span className="text-xs text-ink-3" suppressHydrationWarning>
                    {relativeTime(r.started_at)}
                  </span>
                </div>
                {r.summary && <p className="text-xs text-ink-2">{r.summary}</p>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
