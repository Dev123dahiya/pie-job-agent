import { type LogPage, logsQueryString } from "./logs";
import type {
  AgentStatus,
  DailySchedule,
  LeadSummary,
  ApplicationDetail,
  ApplicationSummary,
  ExecutionModeStatus,
  DashboardOverview,
  DailyLimit,
  HomeStatus,
  Fetched,
  Health,
  JobDetail,
  JobFilterOptions,
  JobList,
  MasterResumeStatus,
  ResumeVersionBrief,
  ResumeVersionDetail,
  RunDetail,
  RunInfo,
  ToolInfo,
} from "./types";

/** Backend base URL, read on the server (never exposed to the browser). */
export function apiBaseUrl(): string {
  return (process.env.PIE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
}

async function getJson<T>(path: string, fetchImpl: typeof fetch = fetch, timeoutMs = 2500): Promise<T> {
  const res = await fetchImpl(`${apiBaseUrl()}${path}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`${path} responded ${res.status}`);
  return (await res.json()) as T;
}

/** Overview from the backend. There is no demo fallback: offline means no numbers at all. */
export function getOverview(fetchImpl?: typeof fetch): Promise<Fetched<DashboardOverview>> {
  return fetched<DashboardOverview>("/api/dashboard/overview", fetchImpl);
}

/** Backend health, or null when the backend cannot be reached at all. */
export async function getHealth(fetchImpl?: typeof fetch): Promise<Health | null> {
  try {
    return await getJson<Health>("/api/health", fetchImpl);
  } catch {
    return null;
  }
}

async function fetched<T>(path: string, fetchImpl?: typeof fetch, timeoutMs?: number): Promise<Fetched<T>> {
  try {
    return { ok: true, data: await getJson<T>(path, fetchImpl, timeoutMs) };
  } catch (err) {
    const offline = err instanceof TypeError || (err instanceof Error && err.name === "TimeoutError");
    return { ok: false, error: offline ? "The backend is not reachable." : String((err as Error).message) };
  }
}

/** Query keys the jobs list accepts; anything else in the page URL is ignored. */
export const JOB_QUERY_KEYS = [
  "q", "location", "source", "freshness", "shortlisted", "min_salary",
  "salary_known_only", "sort", "limit", "offset",
  "status_group", "platform", "apply_type", "company", "min_score", "max_score",
  "found_from", "found_to",
] as const;

export function jobsQueryString(params: Record<string, string | string[] | undefined>): string {
  const qs = new URLSearchParams();
  for (const key of JOB_QUERY_KEYS) {
    const value = params[key];
    const first = Array.isArray(value) ? value[0] : value;
    if (first) qs.set(key, first);
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export function getJobs(params: Record<string, string | string[] | undefined>, fetchImpl?: typeof fetch) {
  return fetched<JobList>(`/api/jobs${jobsQueryString(params)}`, fetchImpl);
}

export function getShortlisted(fetchImpl?: typeof fetch) {
  return fetched<JobList>("/api/jobs/shortlisted?limit=100", fetchImpl);
}

export function getJob(id: string, fetchImpl?: typeof fetch) {
  return fetched<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`, fetchImpl);
}

export function getJobFilters(fetchImpl?: typeof fetch) {
  return fetched<JobFilterOptions>("/api/jobs/filters", fetchImpl);
}

export async function getAgentStatus(fetchImpl?: typeof fetch): Promise<AgentStatus | null> {
  const res = await fetched<AgentStatus>("/api/agent/status", fetchImpl);
  return res.ok ? res.data : null;
}

export async function getAgentConsoleData(fetchImpl?: typeof fetch): Promise<{
  status: AgentStatus | null;
  run: RunDetail | null;
  history: RunInfo[];
  tools: ToolInfo[];
}> {
  const [status, history, tools] = await Promise.all([
    getAgentStatus(fetchImpl),
    fetched<RunInfo[]>("/api/agent/runs?run_type=agent&limit=10", fetchImpl),
    fetched<ToolInfo[]>("/api/agent/tools", fetchImpl),
  ]);
  const runs = history.ok ? history.data : [];
  // Show the live agent run, else one waiting for the user, else the most recent one.
  const active = status?.active_run?.run_type === "agent" ? status.active_run : null;
  const focus = active ?? status?.awaiting_user ?? runs[0] ?? null;
  const run = focus ? await fetched<RunDetail>(`/api/agent/runs/${focus.id}?actions_limit=300`, fetchImpl) : null;
  return {
    status,
    run: run && run.ok ? run.data : null,
    history: runs,
    tools: tools.ok ? tools.data : [],
  };
}

export function getResumeVersions(fetchImpl?: typeof fetch) {
  return fetched<ResumeVersionBrief[]>("/api/resumes/versions?limit=200", fetchImpl);
}

export function getResumeVersion(id: string, fetchImpl?: typeof fetch) {
  return fetched<ResumeVersionDetail>(`/api/resumes/versions/${encodeURIComponent(id)}`, fetchImpl);
}

export function getMasterResumeStatus(fetchImpl?: typeof fetch) {
  return fetched<MasterResumeStatus>("/api/resumes/master", fetchImpl);
}

export const APPLICATION_QUERY_KEYS = ["platform", "status_group", "updated_from", "updated_to"] as const;

export function getApplications(
  params: Record<string, string | string[] | undefined> = {},
  fetchImpl?: typeof fetch,
) {
  const qs = new URLSearchParams();
  for (const key of APPLICATION_QUERY_KEYS) {
    const value = params[key];
    const first = Array.isArray(value) ? value[0] : value;
    if (first) qs.set(key, first);
  }
  const s = qs.toString();
  return fetched<ApplicationSummary[]>(`/api/applications${s ? `?${s}` : ""}`, fetchImpl);
}

export function getApplication(id: string, fetchImpl?: typeof fetch) {
  return fetched<ApplicationDetail>(`/api/applications/${encodeURIComponent(id)}`, fetchImpl);
}

export function getExecutionMode(fetchImpl?: typeof fetch) {
  return fetched<ExecutionModeStatus>("/api/applications/mode", fetchImpl);
}

/** Daily discovery + analysis: schedule and the latest run's report. */
export function getDailyRun(fetchImpl?: typeof fetch) {
  return fetched<DailySchedule>("/api/daily-run", fetchImpl);
}

/** Lead -> trusted ATS resolution state: leads, boards, negative cache, budgets. */
export function getLeadSummary(fetchImpl?: typeof fetch) {
  return fetched<LeadSummary>("/api/leads/summary", fetchImpl);
}

/** Home page: platform modes, live switch, six-status counts and quick links. */
export function getHome(fetchImpl?: typeof fetch) {
  return fetched<HomeStatus>("/api/dashboard/home", fetchImpl);
}

/** Today's live submissions against the daily limits (enforced in the backend). */
export function getDailyLimits(fetchImpl?: typeof fetch) {
  return fetched<DailyLimit[]>("/api/dashboard/limits", fetchImpl);
}

/** The backend log, filtered and redacted by the backend (newest first). */
export function getLogs(params: Record<string, string | string[] | undefined>, fetchImpl?: typeof fetch) {
  return fetched<LogPage>(`/api/logs${logsQueryString(params)}`, fetchImpl, 15_000); // reads the whole log
}
