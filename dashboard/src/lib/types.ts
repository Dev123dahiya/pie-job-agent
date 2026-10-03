// Mirrors backend/services/dashboard.py and backend/api/routes/health.py.

export type FunnelStage = { stage: string; count: number };

export type ActivityItem = {
  occurred_at: string;
  event_type: string;
  reason: string | null;
  new_status: string | null;
};

export type AgentSnapshot = {
  status: string;
  task: string | null;
  started_at: string | null;
  jobs_processed: number;
  applications_submitted: number;
  error_count: number;
};

export type DashboardOverview = {
  jobs_discovered_today: number;
  jobs_shortlisted: number;
  applications_submitted: number;
  assessments: number;
  interviews: number;
  recruiter_responses: number;
  offers: number;
  rejections: number;
  funnel: FunnelStage[];
  recent_activity: ActivityItem[];
  agent: AgentSnapshot;
};

export type ComponentStatus = { status: "ok" | "unavailable" | "not_configured"; detail?: string | null };

export type Health = {
  status: "ok" | "degraded";
  version: string;
  development_mode: boolean;
  components: Record<string, ComponentStatus>;
};


// --- Phase 2: jobs, analysis, agent runs (mirrors backend/api/schemas.py) ---

export type Level = "strong" | "medium" | "weak" | "unknown";
export type Freshness = "fresh" | "stale" | "unknown";

export type RuleResult = { rule: string; passed: boolean; detail: string };

export type Analysis = {
  id: string;
  created_at: string;
  match_score: number | null;
  role_match: Level;
  skills_match: Level;
  experience_match: Level;
  location_match: Level;
  salary_match: Level;
  freshness: Freshness;
  matching_skills: string[];
  missing_skills: string[];
  important_requirements: string[];
  concerns: string[];
  reason: string | null;
  should_shortlist: boolean;
  shortlisted: boolean | null;
  rule_results: RuleResult[];
  analyzer: string | null;
  model: string | null;
  prompt_version: string | null;
};

export type JobSummary = {
  id: string;
  source: string;
  /** One of the six statuses; `status` keeps PIE's detailed state. */
  user_status: UserStatusKey;
  platform: "naukri" | "linkedin" | "company_site";
  apply_type: "company_form" | "naukri_direct" | "linkedin_easy_apply" | "external_link";
  company: string | null;
  title: string;
  location: string | null;
  remote_type: string;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  experience_required: string | null;
  posted_at: string | null;
  discovered_at: string;
  freshness: Freshness;
  status: string;
  url: string;
  times_seen: number;
  last_error: string | null;
  analysis: Analysis | null;
  tailored_version: number | null;
  posted_at_kind?: string | null;
  also_on?: string[];
  salary_period?: string | null;
  salary_note?: string | null;
};

export type TimelineEvent = {
  occurred_at: string;
  event_type: string;
  source: string;
  previous_status: string | null;
  new_status: string | null;
  reason: string | null;
};

export type Readiness = {
  ready: boolean;
  eligible: boolean;
  user_blockers: string[];
  pending_steps: string[];
};

export type ResumeVersionBrief = {
  id: string;
  version_number: number;
  is_master: boolean;
  validation_status: "MASTER" | "VALIDATED" | null;
  tailoring_strategy: string | null;
  artifact_format: string | null;
  job_id: string | null;
  company: string | null;
  role: string | null;
  created_at: string;
  changes_summary: string | null;
  used_by: ResumeUse[];
  locked: boolean;
};

export type ResumeUse = {
  application_id: string;
  job_id: string;
  title: string;
  company: string | null;
  user_status: UserStatusKey;
  locked: boolean;
};

export type ChangeDetails = {
  sections_changed: string[];
  section_order: string[];
  summary_changed: boolean;
  skills_reordered: boolean;
  skills_added: string[];
  skills_removed: string[];
  projects_reordered: boolean;
  projects_removed: string[];
  experience_reordered: boolean;
  bullets_changed: number;
  keywords_emphasized: string[];
  unsupported_claims_rejected: { code: string; detail: string; attempt?: string }[];
  facts_preserved: boolean;
  notes: string[];
};

/** Required vs verified full-time experience. A factual constraint, never concealed. */
export type ExperienceGap = {
  required: string | null;
  required_source: string | null;
  required_min_years: number | null;
  required_max_years: number | null;
  candidate: string;
  candidate_years: number;
  candidate_basis: string;
  internships: string[];
  internships_counted_as_full_time: boolean;
  shortfall_years: number | null;
  status: "gap" | "meets" | "exceeds" | "unknown";
  gap: boolean | "unknown";
  note: string;
};

export type TailoringReport = {
  job_requirements: string[];
  matching_verified_skills: string[];
  matching_experience: string[];
  matching_projects: string[];
  missing_requirements: string[];
  keywords_emphasized: string[];
  sections_changed: string[];
  relevance_improvements: string[];
  /** Absent on versions created before experience-gap reporting. */
  experience_gap?: ExperienceGap;
  note: string;
};

export type ResumeVersionDetail = ResumeVersionBrief & {
  job_title: string | null;
  content: Record<string, unknown> | null;
  change_details: ChangeDetails | null;
  tailoring_report: TailoringReport | null;
  validation_report: { passed: boolean; checks: string[]; attempts?: number } | null;
  source_version_id: string | null;
  source_sha256: string | null;
  model: string | null;
  prompt_version: string | null;
  artifacts: ("pdf" | "markdown")[];
  markdown: string | null;
};

export type MasterResumeStatus = {
  available: boolean;
  file_name: string | null;
  source: string | null;
  sha256: string | null;
  detail: string | null;
};

export type JobDetail = JobSummary & {
  readiness: Readiness | null;
  experience_gap: ExperienceGap | null;
  resume: {
    master_available: boolean;
    tailored_versions: ResumeVersionBrief[];
    latest: ResumeVersionDetail | null;
  } | null;
  application: ApplicationBrief | null;
  description: string | null;
  requirements: string[];
  skills: string[];
  application_url: string | null;
  company_url: string | null;
  last_seen_at: string | null;
  analyses: Analysis[];
  timeline: TimelineEvent[];
};

export type JobList = { items: JobSummary[]; total: number; limit: number; offset: number };

export type JobFilterOptions = {
  sources: string[];
  locations: string[];
  statuses: string[];
  freshness_window_hours: number;
};

export type RunInfo = {
  id: string;
  task: string;
  run_type: string;
  status: string;
  source: string | null;
  search_terms: string[] | null;
  locations: string[] | null;
  current_task: string | null;
  started_at: string;
  ended_at: string | null;
  searches_total: number;
  searches_completed: number;
  jobs_found: number;
  jobs_new: number;
  jobs_duplicate: number;
  jobs_failed: number;
  jobs_analyzed: number;
  jobs_shortlisted: number;
  jobs_rejected: number;
  errors: { error: string; [key: string]: unknown }[];
  summary: string | null;
  model: string | null;
  mode: string | null;
  iterations: number;
  max_iterations: number | null;
  pending_user_input: string | null;
};

export type AgentStatus = {
  state: "idle" | "running" | "stopping";
  active_run: RunInfo | null;
  awaiting_user: RunInfo | null;
  recent_runs: RunInfo[];
  analyzer: "ok" | "not_configured" | "unavailable";
  analyzer_detail: string;
  /** Effective LIVE permission: true only with DEVELOPMENT_MODE=false AND EXTERNAL_LIVE_ENABLED=true. */
  external_live_enabled: boolean;
  external_live_configured: boolean;
  development_mode: boolean;
  default_external_mode: string;
};

export type ExecutionModeStatus = {
  development_mode: boolean;
  external_live_configured: boolean;
  /** Effective: DEVELOPMENT_MODE=false AND EXTERNAL_LIVE_ENABLED=true. */
  external_live_enabled: boolean;
  live_block_reason: string | null;
  browser_headless: boolean;
  default_mode: string;
};

export type RunAction = {
  sequence: number;
  action: string;
  status: string;
  tool: string | null;
  iteration: number | null;
  job_id: string | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  reasoning: string | null;
  error: string | null;
  at: string;
};

export type RunDetail = RunInfo & { actions: RunAction[] };

export type ToolInfo = {
  name: string;
  description: string;
  permission: "READ_ONLY" | "WRITE" | "EXTERNAL_ACTION";
  enabled: boolean;
  input_schema: Record<string, unknown>;
};

export type ApplicationBrief = {
  id: string;
  status: string;
  requires_user_input: boolean;
  user_action_required: string | null;
  applied_at: string | null;
};

/** Result of a server-side fetch: data, or an error explaining why there is none. */
export type Fetched<T> = { ok: true; data: T } | { ok: false; error: string };

export type ExecutionModeName = "read_only" | "internal_writes" | "external_dry_run" | "external_live";

export type SessionField = {
  field_id: string;
  label: string;
  kind: string;
  required: boolean;
  key: string | null;
  category: "VERIFIED_AUTOFILL" | "UNKNOWN" | "AMBIGUOUS" | "SENSITIVE_USER_REQUIRED" | "CHALLENGE";
  action: "fill" | "upload_resume" | "skip" | "block";
  reason: string;
  answer_status: string | null;
  source: string | null;
  has_value?: boolean;
  error?: string | null;
};

export type MissingAnswer = {
  field_id: string;
  question: string;
  question_key: string;
  category: string;
  reason: string;
  answer_status: string | null;
};

export type BrowserSessionView = {
  id: string;
  mode: "DRY_RUN" | "LIVE";
  banner: string;
  state: string;
  current_step: string | null;
  target_url: string;
  target_domain: string;
  current_url: string | null;
  page_title: string | null;
  fields: SessionField[];
  filled_fields: { field_id: string; label: string; key: string; source: string | null; method?: string | null }[];
  skipped_fields: { field_id: string; label: string; reason: string; category?: string }[];
  missing_answers: MissingAnswer[];
  challenges: { type: string; detail: string; user_action: string }[];
  uploaded_resume: { file_name: string; version_number: number; status?: string; detail?: string } | null;
  screenshots: { index: number; label: string; taken_at: string }[];
  errors: { step: string; error: string }[];
  blocked_requests: number;
  user_action_required: string | null;
  prepared_at: string | null;
  user_confirmed_at: string | null;
  submission_evidence: Record<string, unknown> | null;
  dry_run_complete: boolean;
  created_at: string;
  closed_at: string | null;
};

export type ApplicationSummary = {
  id: string;
  job_id: string;
  company: string | null;
  title: string;
  application_status: string;
  job_status: string;
  requires_user_input: boolean;
  applied_at: string | null;
  updated_at: string;
  user_status: UserStatusKey;
  platform: "naukri" | "linkedin" | "company_site";
  resume_locked: boolean;
  latest_session: { id: string; mode: "DRY_RUN" | "LIVE"; state: string; current_step: string | null } | null;
};

export type ApplicationDetail = ApplicationSummary & ExecutionModeStatus & {
  url: string;
  application_url: string | null;
  user_action_required: string | null;
  last_error: string | null;
  resume: {
    version_id: string; version_number: number; is_master: boolean; file_name: string;
    locked: boolean; lock_reason: string | null;
  } | null;
  readiness: Readiness;
  confirmation_statement: string;
  sessions: BrowserSessionView[];
  external_actions: {
    at: string; tool: string; mode: string | null; actor: string; domain: string | null;
    summary: string; result: string; error: string | null; run_id: string | null;
  }[];
  submission_attempts: { id: string; attempted_at: string; outcome: string; evidence: Record<string, unknown> | null }[];
  timeline: { at: string; event_type: string; source: string; previous: string | null; new: string | null; reason: string | null }[];
};

export type DailyJob = {
  job_id: string;
  title: string;
  company: string | null;
  location: string | null;
  posted_at: string | null;
  url: string;
  state: string;
};

/** Report of one daily discovery + analysis run (backend/services/daily.py). */
export type DailyReport = {
  sources: string[];
  counts: Record<
    | "discovered" | "new" | "duplicates" | "fresh" | "stale" | "unknown"
    | "analyzed" | "shortlisted" | "rejected" | "ready_to_apply" | "failures",
    number
  > & { new_fresh_jobs?: number };
  /** Lead -> trusted ATS resolution metrics; only present when lead resolution ran. */
  leads?: Record<string, number>;
  per_source: Record<string, { status: string; error?: string } & Record<string, unknown>>;
  analysis: Record<string, unknown> | null;
  failures: { step: string; error: string }[];
  newly_shortlisted: DailyJob[];
  newly_ready_to_apply: DailyJob[];
};

export type DailySchedule = {
  enabled: boolean;
  time: string;
  timezone: string;
  next_run_at: string | null;
  latest: {
    run_id: string;
    status: string;
    trigger: "scheduled" | "manual";
    started_at: string;
    ended_at: string | null;
    summary: string | null;
    report: DailyReport | null;
  } | null;
};

export type LeadBoard = {
  company: string;
  platform: string;
  board_id: string;
  status: string;
  active: boolean;
  configured: boolean;
  rule: string | null;
  leads: number;
};

/** GET /api/leads/summary: lead -> trusted ATS resolution state (read-only). */
export type LeadSummary = {
  enabled: boolean;
  auto_activate_verified: boolean;
  max_requests_per_run: number;
  max_requests_per_company: number;
  max_companies_per_run: number;
  negative_cache_days: number;
  leads_total: number;
  companies_total: number;
  leads_by_status: Record<string, number>;
  boards_by_status: Record<string, number>;
  negative_cache_active: number;
  boards_needing_review: LeadBoard[];
  active_boards: LeadBoard[];
};

// --- Home page (D1) -------------------------------------------------------------------------

export type UserStatusKey = "found" | "skipped" | "applied" | "needs_input" | "manual" | "failed";
export type StatusCounts = Record<UserStatusKey, number>;

export interface PlatformMode {
  platform: "naukri" | "linkedin" | "company_site";
  mode: "dry-run" | "review" | "auto";
  built: boolean;
  note: string | null;
  login: "logged_in" | "expired" | "never" | null;
}

export interface HomeStatus {
  live_possible: boolean;
  live_note: string;
  platforms: PlatformMode[];
  today: StatusCounts;
  all_time: StatusCounts;
  quick_links: {
    needs_input: number;
    failed: number;
    manual: number;
    waiting_for_review: number | null;
  };
}

export interface DailyLimit {
  platform: "total" | "naukri" | "linkedin" | "company_site";
  used: number;
  limit: number;
  remaining: number;
}
