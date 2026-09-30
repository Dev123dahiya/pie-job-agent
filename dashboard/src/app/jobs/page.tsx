import type { Metadata } from "next";
import Link from "next/link";
import { AgentActivity } from "@/components/agent/AgentActivity";
import { AutomationNotice } from "@/components/jobs/AutomationNotice";
import { PipelineActions } from "@/components/jobs/JobActions";
import { JobFilters } from "@/components/jobs/JobFilters";
import { JobsTable } from "@/components/jobs/JobsTable";
import { getAgentStatus, getJobFilters, getJobs } from "@/lib/api";

export const metadata: Metadata = { title: "Jobs" };
export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;

export default async function JobsPage(props: PageProps<"/jobs">) {
  const params = await props.searchParams;
  const offset = Number(Array.isArray(params.offset) ? params.offset[0] : params.offset) || 0;
  const [jobs, filters, status] = await Promise.all([
    getJobs({ ...params, limit: String(PAGE_SIZE), offset: String(offset) }),
    getJobFilters(),
    getAgentStatus(),
  ]);

  const pageHref = (nextOffset: number) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      const value = Array.isArray(v) ? v[0] : v;
      if (value && k !== "offset") qs.set(k, value);
    }
    if (nextOffset > 0) qs.set("offset", String(nextOffset));
    return `?${qs.toString()}`;
  };

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">Jobs</h1>
          <p className="mt-1 text-sm text-ink-2">
            {jobs.ok
              ? `${jobs.data.total} job${jobs.data.total === 1 ? "" : "s"} discovered, normalized and deduplicated.`
              : "Every posting the agent has discovered."}
          </p>
        </div>
        <PipelineActions />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <AgentActivity initial={status} className="lg:col-span-2" />
        <AutomationNotice />
      </div>

      <JobFilters params={params} options={filters.ok ? filters.data : null} />

      {jobs.ok ? (
        <>
          <JobsTable
            jobs={jobs.data.items}
            emptyMessage="No jobs match these filters. Try Reset, or run Discover jobs."
          />
          {jobs.data.total > PAGE_SIZE && (
            <nav className="flex items-center justify-between text-sm text-ink-2" aria-label="Pagination">
              <span className="tabular">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, jobs.data.total)} of {jobs.data.total}
              </span>
              <div className="flex gap-2">
                {offset > 0 && (
                  <Link href={pageHref(Math.max(0, offset - PAGE_SIZE))} className="rounded-lg border border-border px-3 py-1.5 hover:bg-surface-2">
                    Previous
                  </Link>
                )}
                {offset + PAGE_SIZE < jobs.data.total && (
                  <Link href={pageHref(offset + PAGE_SIZE)} className="rounded-lg border border-border px-3 py-1.5 hover:bg-surface-2">
                    Next
                  </Link>
                )}
              </div>
            </nav>
          )}
        </>
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center">
          <p className="text-sm font-medium text-ink">Jobs could not be loaded</p>
          <p className="mt-1 text-sm text-ink-2">{jobs.error} Start it with uvicorn backend.main:app.</p>
        </div>
      )}
    </div>
  );
}
