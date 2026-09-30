import { FileLock2 } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { getMasterResumeStatus, getResumeVersions } from "@/lib/api";
import { humanize, relativeTime } from "@/lib/format";

export const metadata: Metadata = { title: "Resumes" };
export const dynamic = "force-dynamic";

export default async function ResumesPage() {
  const [master, versions] = await Promise.all([getMasterResumeStatus(), getResumeVersions()]);
  const tailored = versions.ok ? versions.data.filter((v) => !v.is_master) : [];

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Resumes</h1>
        <p className="mt-1 text-sm text-ink-2">
          Your master resume is read-only. Every tailored version is a new, validated file derived from it.
          Internal only. Nothing is submitted.
        </p>
      </div>

      <Card
        title="Master resume"
        description="Never modified, renamed or overwritten by PIE"
        action={
          <StatusBadge
            status={master.ok && master.data.available ? "ok" : "unavailable"}
            label={master.ok && master.data.available ? "Available" : "Not available"}
          />
        }
      >
        {master.ok && master.data.available ? (
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            <div className="flex items-center gap-2 text-ink">
              <FileLock2 aria-hidden className="size-4 text-ink-3" />
              {master.data.file_name}
            </div>
            <div className="truncate text-ink-3" title={master.data.sha256 ?? undefined}>
              SHA-256 {master.data.sha256?.slice(0, 16)}…
            </div>
          </dl>
        ) : (
          <p className="text-sm text-ink-2">
            {master.ok ? master.data.detail : master.error}
          </p>
        )}
      </Card>

      <Card title="Tailored versions" description={`${tailored.length} validated version${tailored.length === 1 ? "" : "s"}`}>
        {!versions.ok ? (
          <p role="alert" className="text-sm text-critical">{versions.error}</p>
        ) : tailored.length === 0 ? (
          <p className="text-sm text-ink-2">
            No tailored resumes yet. Open a shortlisted job and use Create Tailored Resume.
          </p>
        ) : (
          <div className="-mx-4 overflow-x-auto sm:mx-0">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead className="text-xs text-ink-3">
                <tr>
                  <th className="px-3 py-2 font-medium">Job</th>
                  <th className="px-3 py-2 font-medium">Version</th>
                  <th className="px-3 py-2 font-medium">Strategy</th>
                  <th className="px-3 py-2 font-medium">Validation</th>
                  <th className="px-3 py-2 font-medium">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {tailored.map((v) => (
                  <tr key={v.id}>
                    <td className="px-3 py-2">
                      <Link href={`/resumes/${v.id}`} className="font-medium text-ink hover:underline">
                        {v.role ?? "Untitled role"}
                      </Link>
                      <p className="text-xs text-ink-3">{v.company ?? "Unknown company"}</p>
                    </td>
                    <td className="tabular px-3 py-2 text-ink">v{v.version_number}</td>
                    <td className="px-3 py-2 text-ink-2">{humanize(v.tailoring_strategy ?? "—")}</td>
                    <td className="px-3 py-2">
                      <StatusBadge
                        status={v.validation_status === "VALIDATED" ? "ok" : "unknown"}
                        label={v.validation_status === "VALIDATED" ? "Validated" : "Not validated"}
                      />
                    </td>
                    <td className="px-3 py-2 text-ink-3" suppressHydrationWarning>
                      {relativeTime(v.created_at)}
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
