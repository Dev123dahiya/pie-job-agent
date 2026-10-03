import type { DailyLimit, PlatformMode, UserStatusKey } from "./types";

export const PLATFORM_LABEL: Record<DailyLimit["platform"], string> = {
  total: "Total",
  naukri: "Naukri",
  linkedin: "LinkedIn",
  company_site: "Company sites",
};

export const STATUS_LABEL: Record<UserStatusKey, string> = {
  found: "Found",
  skipped: "Skipped",
  applied: "Applied",
  needs_input: "Needs input",
  manual: "Manual",
  failed: "Failed",
};

export const APPLY_TYPE_LABEL = {
  company_form: "Employer form",
  naukri_direct: "Naukri direct apply",
  linkedin_easy_apply: "LinkedIn Easy Apply",
  external_link: "External link",
} as const;

export const STATUS_ORDER: UserStatusKey[] = ["found", "skipped", "applied", "needs_input", "manual", "failed"];

/** Share of a limit used, 0-100. A limit of 0 means switched off: shown as full. */
export function usedPercent(l: DailyLimit): number {
  if (l.limit <= 0) return 100;
  return Math.min(100, Math.round((l.used / l.limit) * 100));
}

export function limitTone(l: DailyLimit): "good" | "warning" | "critical" {
  if (l.remaining === 0) return "critical";
  return usedPercent(l) >= 75 ? "warning" : "good";
}

export function loginText(p: PlatformMode): string {
  if (p.platform === "company_site") return "Not needed (each employer's own site)";
  if (!p.built || p.login === null) return "Available after Phase C";
  const text = { logged_in: "Logged in", expired: "Expired - log in again", never: "Never logged in" };
  return text[p.login];
}
