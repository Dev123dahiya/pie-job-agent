import Link from "next/link";
import { STATUS_LABEL, STATUS_ORDER } from "@/lib/home";

type Params = Record<string, string | string[] | undefined>;
const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? "";
const FIELD =
  "h-9 rounded-lg border border-border bg-surface px-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

/** A plain GET form, like the jobs filters: everything lives in the URL. */
export function ApplicationFilters({ params }: { params: Params }) {
  return (
    <form method="get" aria-label="Filter applications" className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1">
        <span className="text-xs text-ink-3">Platform</span>
        <select name="platform" defaultValue={first(params.platform)} className={FIELD}>
          <option value="">Any</option>
          <option value="naukri">Naukri</option>
          <option value="linkedin">LinkedIn</option>
          <option value="company_site">Company sites</option>
        </select>
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs text-ink-3">Status</span>
        <select name="status_group" defaultValue={first(params.status_group)} className={FIELD}>
          <option value="">Any</option>
          {STATUS_ORDER.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs text-ink-3">Changed from</span>
        <input type="date" name="updated_from" defaultValue={first(params.updated_from)} className={FIELD} />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs text-ink-3">Changed until</span>
        <input type="date" name="updated_to" defaultValue={first(params.updated_to)} className={FIELD} />
      </label>
      <button type="submit" className="h-9 rounded-lg bg-ink px-3 text-sm font-medium text-bg hover:opacity-90">
        Apply filters
      </button>
      <Link href="?" className="h-9 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-surface-2">
        Reset
      </Link>
    </form>
  );
}
