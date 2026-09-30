import type { Metadata } from "next";
import Link from "next/link";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { ExecutionModeBar } from "@/components/applications/ExecutionModeBar";
import { getApplications, getExecutionMode } from "@/lib/api";
import { relativeTime } from "@/lib/format";

export const metadata: Metadata = { title: "Applications" };
export const dynamic = "force-dynamic";

export default async function ApplicationsPage() {
  const [res, mode] = await Promise.all([getApplications(), getExecutionMode()]);
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Applications</h1>
        <p className="mt-1 text-sm text-ink-2">
          Application drafts and their browser execution. Dry runs never submit anything. Live submission needs
          DEVELOPMENT_MODE=false, EXTERNAL_LIVE_ENABLED=true, a visible browser and your explicit confirmation for
          each application.
        </p>
      </div>
      <ExecutionModeBar mode={mode.ok ? mode.data : null} />
      <Card title="All applications">
        {!res.ok ? (
          <p role="alert" className="text-sm text-critical">{res.error}</p>
        ) : res.data.length === 0 ? (
          <p className="text-sm text-ink-2">
            No applications yet. A shortlisted job gets one when you create an application draft.
          </p>
        ) : (
          <div className="-mx-4 overflow-x-auto sm:mx-0">
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead className="text-xs text-ink-3">
                <tr>
                  <th className="px-3 py-2 font-medium">Job</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Browser session</th>
                  <th className="px-3 py-2 font-medium">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {res.data.map((a) => (
                  <tr key={a.id}>
                    <td className="px-3 py-2">
                      <Link href={`/applications/${a.id}`} className="font-medium text-ink hover:underline">
                        {a.title}
                      </Link>
                      <p className="text-xs text-ink-3">{a.company ?? "Unknown company"}</p>
                    </td>
                    <td className="px-3 py-2">
                      <StatusBadge status={a.job_status} />
                      {a.requires_user_input && <p className="mt-1 text-xs text-ink-2">Needs you</p>}
                    </td>
                    <td className="px-3 py-2">
                      {a.latest_session ? (
                        <div className="flex flex-col items-start gap-1">
                          <StatusBadge
                            status={a.latest_session.mode}
                            label={a.latest_session.mode === "LIVE" ? "LIVE" : "DRY RUN"}
                          />
                          <span className="text-xs text-ink-3">{a.latest_session.current_step}</span>
                        </div>
                      ) : (
                        <span className="text-xs text-ink-3">Not started</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-ink-3" suppressHydrationWarning>
                      {relativeTime(a.updated_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
