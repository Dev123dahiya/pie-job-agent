import type { Metadata } from "next";
import { ReviewQueue } from "@/components/review/ReviewQueue";
import { getReviewQueue } from "@/lib/api";

export const metadata: Metadata = { title: "Review queue" };
export const dynamic = "force-dynamic";

export default async function ReviewPage() {
  const res = await getReviewQueue();
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Review queue</h1>
        <p className="mt-1 text-sm text-ink-2">
          Applications filled in a live browser and stopped before submit. Approve to submit for real (every safety
          check runs again: daily limits, duplicates, unchanged form), or reject to submit nothing.
        </p>
      </div>
      {res.ok ? (
        <ReviewQueue items={res.data.items} />
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center text-sm text-ink-2">
          {res.error}
        </div>
      )}
    </div>
  );
}
