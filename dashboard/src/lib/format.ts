const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function relativeTime(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((new Date(iso).getTime() - now.getTime()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 60) return rtf.format(seconds, "second");
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), "hour");
  return rtf.format(Math.round(seconds / 86400), "day");
}

/** "APPLICATION_CONFIRMED" -> "Application confirmed" */
export function humanize(value: string): string {
  const s = value.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function percent(part: number, whole: number): string {
  if (whole <= 0) return "—";
  return `${Math.round((part / whole) * 100)}%`;
}

const compact = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, ""));

/** Salary as published. INR in lakhs per annum; other currencies in thousands. Never converted. */
export function formatSalary(min: number | null, max: number | null, currency: string | null): string {
  if (min == null && max == null) return "Not listed";
  const cur = currency ?? "";
  const lo = min ?? max!;
  const hi = max ?? min!;
  if (cur === "INR") {
    const a = compact(lo / 100_000);
    const b = compact(hi / 100_000);
    return `₹${a === b ? a : `${a}–${b}`} LPA`;
  }
  const symbol = cur === "USD" ? "$" : cur === "EUR" ? "€" : cur === "GBP" ? "£" : `${cur} `;
  const a = compact(lo / 1000);
  const b = compact(hi / 1000);
  return `${symbol}${a === b ? a : `${a}–${b}`}k`;
}

export const FRESHNESS_LABEL: Record<string, string> = {
  fresh: "Fresh",
  stale: "Stale",
  unknown: "No post date",
};

/** What a posting's date means, per source. An update time is never shown as a post date. */
export const POSTED_KIND_LABEL: Record<string, string> = {
  published: "published",
  released: "released",
  created: "created (published on or after)",
  published_tz_unknown: "published (time zone not stated: shown as oldest possible)",
  updated_only: "only an update time is known",
  unknown: "no post date from the source",
};
