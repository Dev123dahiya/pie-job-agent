import type { Fetched } from "./types";

export type LogLine = { at: string; level: string; logger: string; message: string };
export type LogPage = { items: LogLine[]; total: number; limit: number; offset: number };

/** Query keys the logs API accepts; anything else in the page URL is ignored. */
export const LOG_QUERY_KEYS = ["q", "level", "run", "platform", "date_from", "date_to", "offset"] as const;

export function logsQueryString(params: Record<string, string | string[] | undefined>, drop: string[] = []): string {
  const qs = new URLSearchParams();
  for (const key of LOG_QUERY_KEYS) {
    if (drop.includes(key)) continue;
    const value = params[key];
    const first = Array.isArray(value) ? value[0] : value;
    if (first) qs.set(key, first);
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

/** The filtered log as a .txt download, through the dashboard's backend proxy. */
export function logsDownloadUrl(params: Record<string, string | string[] | undefined>): string {
  return `/backend/api/logs/download${logsQueryString(params, ["offset"])}`;
}

export const LEVEL_TONE: Record<string, string> = {
  DEBUG: "text-ink-3",
  INFO: "text-ink-2",
  WARNING: "text-warning",
  ERROR: "text-critical",
  CRITICAL: "text-critical",
};

export type { Fetched };
