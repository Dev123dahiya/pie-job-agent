import type { Metadata } from "next";
import { ManualList } from "@/components/manual/ManualList";
import { getManualJobs } from "@/lib/api";

export const metadata: Metadata = { title: "Manual apply" };
export const dynamic = "force-dynamic";

export default async function ManualApplyPage() {
  const res = await getManualJobs();
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Manual apply</h1>
        <p className="mt-1 text-sm text-ink-2">
          Jobs to apply to yourself: their site isn&apos;t supported, or you chose to. Apply on the site, then mark
          them applied so they are tracked like the others. These don&apos;t count toward PIE&apos;s daily limits.
        </p>
      </div>
      {res.ok ? (
        <ManualList items={res.data.items} />
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
          {res.error}
        </div>
      )}
    </div>
  );
}
