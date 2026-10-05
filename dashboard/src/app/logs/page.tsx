import { Download } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { LogsTable } from "@/components/logs/LogsTable";
import { getLogs } from "@/lib/api";
import { logsDownloadUrl, logsQueryString } from "@/lib/logs";

export const metadata: Metadata = { title: "Logs" };
export const dynamic = "force-dynamic";

const PAGE_SIZE = 200;
const FIELD =
  "h-9 rounded-lg border border-border bg-surface px-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? "";

export default async function LogsPage(props: PageProps<"/logs">) {
  const params = await props.searchParams;
  const offset = Number(first(params.offset)) || 0;
  const res = await getLogs({ ...params, offset: String(offset) });
  const pageHref = (next: number) =>
    logsQueryString({ ...params, offset: next > 0 ? String(next) : undefined }) || "?";

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">Logs</h1>
          <p className="mt-1 text-sm text-ink-2">
            The backend log, newest first. Passwords, tokens, keys and cookies are always redacted.
          </p>
        </div>
        <a
          href={logsDownloadUrl(params)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-2 text-sm font-medium text-ink hover:bg-surface-2"
        >
          <Download aria-hidden className="size-4" /> Download filtered log
        </a>
      </div>

      <form method="get" aria-label="Filter logs" className="rounded-xl border border-border bg-surface p-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <label className="col-span-2 flex flex-col gap-1">
            <span className="text-xs text-ink-3">Search</span>
            <input name="q" defaultValue={first(params.q)} maxLength={200} placeholder="text in the message" className={FIELD} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-3">Level (and above)</span>
            <select name="level" defaultValue={first(params.level)} className={FIELD}>
              <option value="">Any</option>
              {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-3">Run id</span>
            <input name="run" defaultValue={first(params.run)} maxLength={64} pattern="[0-9a-fA-F\-]*" placeholder="Any" className={FIELD} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-3">Platform</span>
            <select name="platform" defaultValue={first(params.platform)} className={FIELD}>
              <option value="">Any</option>
              <option value="naukri">Naukri</option>
              <option value="linkedin">LinkedIn</option>
              <option value="instahyre">Instahyre</option>
              <option value="company_site">Company sites</option>
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-3">From</span>
              <input type="date" name="date_from" defaultValue={first(params.date_from)} className={FIELD} />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-ink-3">Until</span>
              <input type="date" name="date_to" defaultValue={first(params.date_to)} className={FIELD} />
            </label>
          </div>
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <Link href="?" className="rounded-lg px-3 py-1.5 text-sm text-ink-2 hover:bg-surface-2">
            Reset
          </Link>
          <button type="submit" className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-bg hover:opacity-90">
            Apply filters
          </button>
        </div>
        <p className="mt-2 text-xs text-ink-3">The platform filter matches log lines that name the platform.</p>
      </form>

      {res.ok ? (
        <>
          <p className="text-sm text-ink-2">
            {res.data.total} line{res.data.total === 1 ? "" : "s"}
          </p>
          <LogsTable items={res.data.items} />
          {res.data.total > PAGE_SIZE && (
            <nav className="flex items-center justify-between text-sm text-ink-2" aria-label="Pagination">
              <span className="tabular">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, res.data.total)} of {res.data.total}
              </span>
              <div className="flex gap-2">
                {offset > 0 && (
                  <Link href={pageHref(Math.max(0, offset - PAGE_SIZE))} className="rounded-lg border border-border px-3 py-1.5 hover:bg-surface-2">
                    Newer
                  </Link>
                )}
                {offset + PAGE_SIZE < res.data.total && (
                  <Link href={pageHref(offset + PAGE_SIZE)} className="rounded-lg border border-border px-3 py-1.5 hover:bg-surface-2">
                    Older
                  </Link>
                )}
              </div>
            </nav>
          )}
        </>
      ) : (
        <div role="alert" className="rounded-xl border border-border bg-surface px-6 py-10 text-center">
          <p className="text-sm font-medium text-ink">Logs could not be loaded</p>
          <p className="mt-1 text-sm text-ink-2">{res.error}</p>
        </div>
      )}
    </div>
  );
}
