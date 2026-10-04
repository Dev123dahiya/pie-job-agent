export type RunStep = "discover" | "analyze" | "tailor" | "apply";
export type RunMode = "dry-run" | "review" | "auto";
export type Platform = "naukri" | "linkedin" | "company_site";

export type RunOptions = {
  steps: RunStep[];
  platforms: {
    platform: Platform;
    apply_supported: boolean;
    review_confirmation: { platform: Platform; remaining: number; phrase: string };
    auto_confirmation?: { platform: Platform; remaining: number; phrase: string };
    auto?: { platform: Platform; unlocked: boolean; missing: string[] };
  }[];
  modes: RunMode[];
  default_mode: RunMode;
  live_possible: boolean;
  live_note: string | null;
  max_analysis: number;
};

export type RunSummary = {
  id: string;
  task: string;
  run_type: string;
  status: string;
  mode: string | null;
  platforms: string | null;
  current_task: string | null;
  started_at: string;
  ended_at: string | null;
  steps_total: number;
  steps_done: number;
  jobs_processed: number;
  summary: string | null;
  errors: { at?: string; error: string }[];
};

export type PipelineRunDetail = RunSummary & {
  report: { counts?: Record<string, number>; failures?: { step: string; error: string }[] } | null;
  progress: Record<string, unknown> | null;
};

export const ACTIVE_RUN_STATUSES = new Set(["RUNNING", "STOPPING", "PENDING"]);

export const STEP_LABEL: Record<RunStep, string> = {
  discover: "Discover jobs",
  analyze: "Analyze & shortlist (Claude)",
  tailor: "Tailor resumes (Claude)",
  apply: "Apply",
};

/** Review mode is live: the exact phrase the backend expects for each chosen platform. */
export function reviewPhrases(options: RunOptions, platforms: Platform[], mode: RunMode = "review"): string[] {
  return options.platforms
    .filter((p) => platforms.includes(p.platform))
    .map((p) => (mode === "auto" ? p.auto_confirmation?.phrase : p.review_confirmation.phrase) ?? "");
}

/** Auto mode is usable only on ONE platform that has unlocked it (REQUIREMENTS 10). */
export function autoBlocker(options: RunOptions, platforms: Platform[]): string | null {
  if (platforms.length !== 1) return "Choose exactly one platform for auto mode.";
  const p = options.platforms.find((x) => x.platform === platforms[0]);
  if (!p?.auto || p.auto.unlocked) return null;
  return `Auto unlocks on this platform after ${p.auto.missing.join(" and ")}.`;
}

export type ReviewItem = {
  session_id: string;
  application_id: string;
  job_id: string;
  title: string;
  company: string | null;
  platform: Platform;
  url: string;
  match_score: number | null;
  match_reason: string | null;
  resume: { version_id: string; version_number: number } | null;
  answers: { label: string | null; key: string | null; source: string | null; ai_suggested: boolean }[];
  uploaded_resume: string | null;
  prepared_at: string | null;
  confirmation_statement: string;
};
