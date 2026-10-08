import type { Metadata } from "next";
import { FailedList } from "@/components/failed/FailedList";
import { getFailedJobs } from "@/lib/api";

export const metadata: Metadata = { title: "Failed" };
export const dynamic = "force-dynamic";

export default async function FailedPage() {
  const res = await getFailedJobs();
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Failed</h1>
        <p className="mt-1 text-sm text-ink-2">
          Applications that failed in the browser, with the error, the step and a screenshot. PIE never retries them
          by itself: you decide.
        </p>
      </div>
      {res.ok ? (
        <FailedList items={res.data.items} />
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
          {res.error}
        </div>
      )}
    </div>
  );
}
