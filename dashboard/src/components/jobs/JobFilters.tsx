import Link from "next/link";
import { APPLY_TYPE_LABEL, STATUS_LABEL, STATUS_ORDER } from "@/lib/home";
import type { JobFilterOptions } from "@/lib/types";

type Params = Record<string, string | string[] | undefined>;

const first = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v) ?? "";

const FIELD =
  "h-9 rounded-lg border border-border bg-surface px-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-ink-3">{label}</span>
      {children}
    </label>
  );
}

/** A plain GET form: filters live in the URL, work without JavaScript and can be shared. */
export function JobFilters({ params, options }: { params: Params; options: JobFilterOptions | null }) {
  return (
    <form method="get" className="rounded-xl border border-border bg-surface p-4" aria-label="Filter jobs">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <div className="col-span-2">
          <Field label="Role or company">
            <input name="q" defaultValue={first(params.q)} placeholder="e.g. backend, MongoDB" className={FIELD} />
          </Field>
        </div>
        <Field label="Location">
          <input name="location" list="job-locations" defaultValue={first(params.location)} placeholder="Any" className={FIELD} />
          <datalist id="job-locations">
            {options?.locations.map((l) => <option key={l} value={l} />)}
          </datalist>
        </Field>
        <Field label="Source">
          <select name="source" defaultValue={first(params.source)} className={FIELD}>
            <option value="">Any</option>
            {options?.sources.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </Field>
        <Field label={`Freshness (${options?.freshness_window_hours ?? 48}h)`}>
          <select name="freshness" defaultValue={first(params.freshness)} className={FIELD}>
            <option value="">Any</option>
            <option value="fresh">Fresh</option>
            <option value="stale">Stale</option>
            <option value="unknown">No post date</option>
          </select>
        </Field>
        <Field label="Shortlisted">
          <select name="shortlisted" defaultValue={first(params.shortlisted)} className={FIELD}>
            <option value="">Any</option>
            <option value="true">Shortlisted</option>
            <option value="false">Not shortlisted</option>
          </select>
        </Field>
        <Field label="Min. salary (INR/yr)">
          <select name="min_salary" defaultValue={first(params.min_salary)} className={FIELD}>
            <option value="">Any</option>
            <option value="500000">5 LPA</option>
            <option value="1000000">10 LPA</option>
            <option value="1500000">15 LPA</option>
            <option value="2000000">20 LPA</option>
          </select>
        </Field>
        <Field label="Sort">
          <select name="sort" defaultValue={first(params.sort) || "newest"} className={FIELD}>
            <option value="newest">Newest found</option>
            <option value="posted">Newest posted</option>
            <option value="match">Best match</option>
            <option value="salary">Highest salary</option>
            <option value="company">Company A–Z</option>
          </select>
        </Field>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <Field label="Status">
          <select name="status_group" defaultValue={first(params.status_group)} className={FIELD}>
            <option value="">Any</option>
            {STATUS_ORDER.map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Platform">
          <select name="platform" defaultValue={first(params.platform)} className={FIELD}>
            <option value="">Any</option>
            <option value="naukri">Naukri</option>
            <option value="linkedin">LinkedIn</option>
            <option value="company_site">Company sites</option>
          </select>
        </Field>
        <Field label="Apply type">
          <select name="apply_type" defaultValue={first(params.apply_type)} className={FIELD}>
            <option value="">Any</option>
            {Object.entries(APPLY_TYPE_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Company">
          <input name="company" defaultValue={first(params.company)} placeholder="Any" maxLength={200} className={FIELD} />
        </Field>
        <Field label="Min. match score">
          <input name="min_score" type="number" min={0} max={100} defaultValue={first(params.min_score)} placeholder="0" className={FIELD} />
        </Field>
        <Field label="Max. match score">
          <input name="max_score" type="number" min={0} max={100} defaultValue={first(params.max_score)} placeholder="100" className={FIELD} />
        </Field>
        <Field label="Found from">
          <input name="found_from" type="date" defaultValue={first(params.found_from)} className={FIELD} />
        </Field>
        <Field label="Found until">
          <input name="found_to" type="date" defaultValue={first(params.found_to)} className={FIELD} />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-xs text-ink-2">
          <input type="checkbox" name="salary_known_only" value="true" defaultChecked={first(params.salary_known_only) === "true"} />
          Only jobs that publish a salary (otherwise unknown salaries are kept)
        </label>
        <div className="flex gap-2">
          <Link href="?" className="rounded-lg px-3 py-1.5 text-sm text-ink-2 hover:bg-surface-2">
            Reset
          </Link>
          <button type="submit" className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-bg hover:opacity-90">
            Apply filters
          </button>
        </div>
      </div>
    </form>
  );
}
