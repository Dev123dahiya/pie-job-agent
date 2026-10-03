export type RunStep = "discover" | "analyze" | "tailor" | "apply";
export type RunMode = "dry-run" | "review";
export type Platform = "naukri" | "linkedin" | "company_site";

export type RunOptions = {
  steps: RunStep[];
  platforms: {
    platform: Platform;
    apply_supported: boolean;
    review_confirmation: { platform: Platform; remaining: number; phrase: string };
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
export function reviewPhrases(options: RunOptions, platforms: Platform[]): string[] {
  return options.platforms.filter((p) => platforms.includes(p.platform)).map((p) => p.review_confirmation.phrase);
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
