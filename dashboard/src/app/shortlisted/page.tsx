import type { Metadata } from "next";
import { AutomationNotice } from "@/components/jobs/AutomationNotice";
import { PipelineActions } from "@/components/jobs/JobActions";
import { JobsTable } from "@/components/jobs/JobsTable";
import { getShortlisted } from "@/lib/api";

export const metadata: Metadata = { title: "Shortlisted" };
export const dynamic = "force-dynamic";

export default async function ShortlistedPage() {
  const res = await getShortlisted();
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">Shortlisted</h1>
          <p className="mt-1 text-sm text-ink-2">
            Jobs that passed every shortlist rule, best match first. Open one to see why.
          </p>
        </div>
        <PipelineActions />
      </div>
      <AutomationNotice />
      {res.ok ? (
        <JobsTable
          jobs={res.data.items}
          emptyMessage="Nothing shortlisted yet. Discover jobs, then run Analyze & shortlist."
        />
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
          {res.error}
        </div>
      )}
    </div>
  );
}
