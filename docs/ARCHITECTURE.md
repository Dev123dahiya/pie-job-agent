# PIE Job Agent — Architecture

PIE (Personal Intelligence Engine) is a personal assistant platform; the **Job Agent** is its first module.
It finds fresh jobs, decides which are worth applying to, tailors a resume, applies through a browser
where it is permitted to, and tracks every outcome through email. It never invents facts.

## Layers

```
USER
 │
 ▼
PIE DASHBOARD  (dashboard/ — Next.js)          UI only: shows state, sends commands
 │  HTTP/JSON
 ▼
PIE BACKEND / ORCHESTRATOR  (backend/ — FastAPI) API, run control, permissions, persistence
 │
 ▼
CLAUDE AGENT  (agent/)                          decides WHAT to do next, and why
 │  structured tool calls (validated in and out)
 ▼
TOOLS
 ├── Browser        (browser/ — Playwright)     HOW to click, type, upload…
 ├── Job sources    (integrations/job_sources/) one adapter per site, via the JobSource interface
 ├── Gmail          (integrations/gmail/)       OAuth, targeted queries only
 ├── Resume/files   (resumes/)                  master is read-only; tailored copies are versioned
 ├── Database       (database/)                 SQLAlchemy models + repositories
 └── Excel          (integrations/excel/)       XLSX exports generated from the DB
```

**Principles**

1. **The dashboard is not the agent.** It only reads state and sends commands.
2. **Claude reasons; tools act.** The browser, Gmail and database tools have no model logic, so each can be tested alone.
3. **Claude-specific code stays in `agent/`.** The rest of the system talks to an agent interface, so the model can be replaced.
4. **The database is the source of truth.** Excel is only an export, and YAML holds only user-editable configuration.
5. **Everything is auditable.** Every agent run (`AgentRun`) and tool call (`AgentAction`) is stored, and so is every status change (`ApplicationEvent`, with its reason and evidence).
6. **Truthfulness is structural.** Missing salary or experience data is stored as `unknown`, never as a guess. A question the verified profile cannot answer sets `requires_user_input`. `SUBMITTED` is set only when the browser confirms the submission.
7. **The agent never bypasses protections.** A CAPTCHA, OTP, MFA or login wall moves the job to `BLOCKED_NEEDS_USER` with the exact action the user must take, and the agent moves on to the next job.

## Repository layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI app (`main.py`), settings (`core/config.py`), profile loader (`core/profile.py`), routes (`api/routes/`), services (`services/`), scripts |
| `database/` | Declarative base (`base.py`), enums, models, repositories, pipeline state machine, Alembic migrations |
| `agent/` | Claude agent: system prompts, tool schemas, execution loop, workflows *(Phase 3)* |
| `browser/` | Playwright tools: `open_page`, `click`, `type`, `upload_file`, `detect_application_form`… *(Phase 5)* |
| `integrations/` | `job_sources/` *(Phase 2)*, `gmail/` *(Phase 6)*, `excel/` *(Phase 7)* |
| `config/` | `user_profile.example.yaml` (committed template) → `user_profile.yaml` (git-ignored, the user's real data) |
| `resumes/` | `master/` (source of truth, never overwritten), `tailored/` (per-job versions) |
| `data/` | `logs/`, `exports/` (git-ignored contents) |
| `dashboard/` | Next.js 16 + TypeScript + Tailwind 4 |
| `tests/` | pytest: `unit/`, `integration/` |

## Data model

| Model | Purpose / key points |
|---|---|
| `User` | The owner. Detailed profile lives in YAML so it can be edited without code changes. |
| `Company` | Unique `normalized_name`, used for dedupe and email matching. |
| `Job` | A normalized posting. Unique on `(source, source_job_id)`. Also stores `canonical_url` and `fingerprint` for secondary dedupe, and `times_seen`/`last_seen_at` for repeat sightings. `pipeline_state` follows the state machine. Salary and experience are nullable (unknown, never guessed). `last_error` holds the latest analysis failure. |
| `JobAnalysis` | One validated analysis. Stores each dimension (`role/skills/experience/location/salary_match`: strong/medium/weak/unknown), `freshness` (fresh/stale/unknown), skills, requirements, concerns, `match_score`, the analyzer's `should_shortlist` and `reason`. Also stores provenance (`analyzer`, `model`, `prompt_version`, `input_snapshot`) and the rule outcome (`shortlisted`, `rule_results`). Re-analysis adds rows; history is kept. |
| `Resume` / `ResumeVersion` | Versions are immutable files. A tailored version links to `source_version_id` (its master), `job_id`, company and role, plus a `changes_summary`. |
| `Application` | One per job. `status` follows the §18 lifecycle. `applied_at` is set only on confirmed submission. `requires_user_input` and `user_action_required` record what the user must do. |
| `ApplicationEvent` | The timeline. Keyed on `job_id` so it starts at discovery, before any application exists; `application_id` is optional. Status changes carry previous/new status, source, reason and evidence (JSON), and can link to the email or agent action that caused them. |
| `Email` | Unique `provider_message_id`, with a classification. Links to an application only with a `match_confidence`; low-confidence matches set `needs_review`. |
| `AgentRun` / `AgentAction` | A run has a type (discovery or analysis), its parameters (source, terms, locations), live progress (`current_task`, searches done/total, a heartbeat), and counters (found, new, duplicate, failed, analyzed, shortlisted, rejected). Each run has an ordered action log whose codes include `DISCOVERY_STARTED`, `JOB_DISCOVERED`, `JOB_DUPLICATE`, `JOB_NORMALIZED`, `JOB_ANALYSIS_*`, `JOB_SHORTLISTED`, `JOB_REJECTED` and `RUN_CANCELLED`. Development-mode side effects are logged as `DRY_RUN`. |

**Two status machines, deliberately separate:**

- `JobPipelineState` (§14) tracks what the agent is doing with a job: `DISCOVERED → ANALYZED → SHORTLISTED → READY_TO_APPLY → APPLICATION_IN_PROGRESS → SUBMITTED`, plus `REJECTED_BY_AGENT`, `BLOCKED_NEEDS_USER`, `FAILED`, `DUPLICATE` and `WITHDRAWN`. The allowed transitions are in `database/state_machine.py`.
- `ApplicationStatus` (§18) tracks what the employer is doing after submission: `APPLIED → APPLICATION_CONFIRMED → ASSESSMENT → INTERVIEW → OFFER`, and so on. It is mostly driven by email.

**Portability.** The models use portable SQLAlchemy types:

- `Uuid`
- `JSON`, which becomes `JSONB` on PostgreSQL
- enums stored as `VARCHAR` without a native DB enum, so adding a value needs no migration

PIE runs on SQLite by default (`data/pie.db`, WAL mode, a busy timeout, and SAVEPOINTs always inside a real transaction; see `database/session.py`); PostgreSQL is optional. A migration test checks that `alembic upgrade head` produces exactly the schema the models declare, on SQLite always and on PostgreSQL when `PIE_TEST_POSTGRES_URL` is set.

## Autonomous agent (Phase 3)

```
Dashboard /agent -> POST /api/agent/runs -> AgentRunner (backend/services/agent_runner.py)
   loop: context -> Claude decision (agent/decision.py) -> ToolRegistry.execute (agent/tools)
         -> AgentAction events -> repeat until complete / needs user / cancelled / limits
```

- Claude decides; PIE executes. The only actions available are registered tools, each with an input and output schema and a permission level (`READ_ONLY`, `WRITE`, `EXTERNAL_ACTION`).
- `EXTERNAL_ACTION` is blocked by a hard-coded constant before any handler runs. **External application submission is not enabled.**
- Runs are rows in `agent_runs` (`run_type=agent`, `mode`, `iterations`, `max_iterations`, `pending_user_input`). Every step is a row in `agent_actions` with a structured event code. The history is rebuilt from the database, so runs survive refreshes and restarts and can be resumed.
- One active run at a time, across discovery, analysis and agent runs.
- Pipeline states are consolidated to `DISCOVERED → ANALYZED → SHORTLISTED → READY_TO_APPLY → APPLICATION_IN_PROGRESS → SUBMITTED`, plus `REJECTED_BY_AGENT`, `BLOCKED_NEEDS_USER`, `FAILED`, `DUPLICATE` and `WITHDRAWN` (migration `0003_phase3`).

Full details are in [AGENT.md](AGENT.md).

## Resume tailoring (Phase 4)

```
create_tailored_resume (tool) / POST /api/jobs/{id}/tailored-resume
  -> backend/services/tailoring.py
     master resume text (read-only, pypdf) + verified profile + job + analysis
     -> Claude structured output (agent/tailoring/schema.py, prompt tailor-v2)
     -> validation against evidence (agent/tailoring/validation.py)
        -> on violations: one regeneration with feedback -> still failing: nothing saved
     -> new ResumeVersion (VALIDATED) + .md/.pdf artifacts (exclusive create, never overwrite)
     -> change summary + tailoring report (agent/tailoring/report.py, deterministic)
     -> readiness re-evaluated (backend/services/readiness.py)
```

- The header and contact block come from the verified profile, never from Claude.
- `resume_versions` gains provenance and validation columns (migration `0004_phase4`): `validation_status`, `validation_report`, `tailoring_strategy`, `artifact_format`, `content`, `change_details`, `tailoring_report`, `source_sha256`, `profile_sha256`, `model` and `prompt_version`. The master is registered as a `MASTER` version keyed by its SHA-256.
- Readiness: missing master resume or standard answers → `BLOCKED_NEEDS_USER`; missing draft or validated resume → stays `SHORTLISTED`; everything present → `READY_TO_APPLY`. Tailoring is an internal write; no external action is enabled.

## Browser execution (Phase 5)

```
agent tools (agent/tools/execution.py)      dashboard (/api/applications/...)
            \                                /
             backend/services/execution.py  (gates, state, audit, idempotency)
               |- agent/execution/answers.py   answer bank: VERIFIED / UNCONFIRMED / UNKNOWN
               |- agent/execution/mapping.py   field -> answer key (strict; never guesses)
               |- integrations/browser/        Playwright on one dedicated thread
                    manager.py  fixed operations only; dry-run guard aborts non-GET requests
                    dom.py      the only JavaScript PIE runs (form extraction, a constant)
                    signals.py  CAPTCHA/OTP/2FA/login detection, confirmation, URL redaction
```

- Pipeline: `READY_TO_APPLY -> APPLICATION_IN_PROGRESS -> READY_FOR_SUBMISSION -> SUBMITTED`. `BLOCKED_NEEDS_USER` and `FAILED` are the side exits. Closing a session without submitting returns the job to `READY_TO_APPLY`. The one state machine lives in `database/state_machine.py`.
- Execution modes: `read_only`, `internal_writes`, `external_dry_run` (the default for external work) and `external_live` (requires `EXTERNAL_LIVE_ENABLED=true`).
- Tables (migration `0005_phase5`):
  - `browser_sessions`: mode, state, target, detected/filled/skipped fields, missing answers, challenges, screenshots, errors, blocked requests, confirmation and evidence.
  - `external_action_logs`: every external action, including refusals.
  - `submission_attempts`: one unique idempotency key per application, committed before the click.
  - `application_answers`: answers the user supplied.
- A submission is only `SUBMITTED` on positive evidence (success message and/or application id). Ambiguous results block for the user and are never retried.
- Live requires `DEVELOPMENT_MODE=false` + `EXTERNAL_LIVE_ENABLED=true` + `BROWSER_HEADLESS=false`. `DEVELOPMENT_MODE=true` always yields `DEVELOPMENT_MODE_BLOCKED` (see `live_execution_block`). `GET /api/applications/mode` reports the effective mode; the dashboard shows it on the Agent and Applications pages.
- Fields are classified as `VERIFIED_AUTOFILL`, `UNKNOWN`, `AMBIGUOUS`, `SENSITIVE_USER_REQUIRED` or `CHALLENGE`; only the first is filled. Custom dropdowns are selected only on a clear match and then verified.
- Challenge detection reads interactive UI text only (never the job description). After a restart, pages are reloaded and form state is rebuilt from the session record.

## Job sources

Adapters live in `integrations/job_sources/`: `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `remotive`, `adzuna` and `mock`.
- Each implements `JobSource`: `plan`, `search_jobs`, `get_job`, `normalize_job` and `enrich`, plus a `describe()` (discovery method, docs URL, timestamp kind, server-side search, pagination).
- The new adapters share `http.PoliteHttp`: delay, bounded retries for network errors, timeouts and 5xx; stop-for-the-run on 429; 401/403 = auth required; 404 = missing; an optional per-run request budget.
- Normalized jobs carry `posted_at_kind`, `source_updated_at`, `salary_period` and `salary_raw`. Duplicates across sources append to `source_attributions` (migration `0006_sources`).
- Discovery logs, per search and per run, how many postings were scanned, rejected by role, rejected by location, new and duplicate.

### Sources investigated for trustworthy freshness (Phase 5.4)

Only a documented publication/release time (or a creation time that necessarily precedes
publication) may make a job FRESH. Findings from the official documentation:

| Source | Why not used (or used) |
|---|---|
| Workable public endpoint | `published_on` = "The publication date of the job" (date only) qualifies, but 0 of 71 Indian company accounts checked had any published job - no India coverage, so not implemented |
| Recruitee Careers Site API | `published_at` has no documented meaning; a Careers API token becomes mandatory in Feb 2027 |
| Himalayas | `pubDate` is when Himalayas published it; it aggregates other sites (same problem as Adzuna) |
| Remote OK, We Work Remotely, The Muse | date fields have no documented meaning |
| Personio XML | only `createdAt`, meaning not documented; Europe-focused |
| Breezy `/json`, Teamtailor | not documented / docs unreadable |
| Freshteam, Keka Hire | need the employer's own credentials |
| Jooble | aggregator; fields not documented |
| Adzuna | `created` is the source's date OR Adzuna's first-seen time, with no way to tell: kept UNKNOWN |

Coverage was instead added on the trusted ATSs: SmartRecruiters boards `ixigo` and
`irissoftware` (active NCR postings).

## Discovery pipeline (Phase 2)

```
SearchQuery (config/job_sources.yaml)
  -> JobSource.plan()          one SearchTask per board / term ...
  -> JobSource.search_jobs()   raw postings        (task failure: logged, run continues)
  -> JobSource.normalize_job() JobCandidate        (bad posting: counted as failed)
  -> filter                    title ~ terms/aliases AND location ~ configured patterns
  -> dedupe                    source+id  ->  canonical URL  ->  fingerprint
  -> JobSource.enrich()        optional detail fetch (e.g. published pay range)
  -> store                     Job + "job_discovered" timeline event
```

- **Adapters** live in `integrations/job_sources/` and register in `registry.py`. The core only sees `JobCandidate`. Adapters must be read-only and must respect site rules. Login walls raise `SourceAuthRequired` and are never bypassed. A `429` stops that source for the rest of the run.
- **Greenhouse** (`greenhouse.py`) uses the official public Job Board API. It makes one request per board, sends an honest User-Agent, waits between requests, and uses bounded retries. It uses `first_published` as the posting date (`updated_at` is not one). Salary comes only from a single published pay range; otherwise it stays null. Currencies are never converted.
- **Mock** (`mock.py` + `fixtures/mock_jobs.json`) is offline and fictional. It covers a strong match, weak match, missing salary, missing experience, URL and fingerprint duplicates, an old job, a remote job, a non-technical role, a wrong location, a malformed record and an undated job.
- **Runs**: only one discovery or analysis run is active at a time (a second request gets `409`). Progress is written as it happens. A run whose heartbeat is older than 10 minutes is closed as FAILED. Cancellation takes effect at the next safe point.

## Analysis and shortlisting (Phase 2)

- **Input.** Structured job fields only, never HTML. `freshness` is computed by PIE. The prompt (`agent/analysis/prompt.py`, versioned) has three separate sections: `VERIFIED_PROFILE` (facts), `PREFERENCES` (wishes) and `JOB`.
- **Output.** JSON matching `JobAnalysisOutput`, with extra keys forbidden and strict types. The request asks for it through structured outputs, and it is validated with Pydantic. `validate_output` also rejects ungrounded answers:
  - `matching_skills` not in the verified profile
  - `salary_match` other than `unknown` when the job states no salary
  - `experience_match` other than `unknown` when the job gives no experience information
  - a `freshness` value that differs from the one PIE computed

  A rejected output is retried once with the validation error as feedback. After that the analysis fails, `job.last_error` is set, and nothing is stored.
- **Backends** (`agent/analysis/analyzers.py`):
  - `claude_api`: Anthropic SDK with an `output_config.format` JSON schema and the server-side refusal fallback.
  - `claude_cli`: `claude -p --json-schema` with no tools, no MCP servers and no settings; it uses the user's Claude subscription.
  - `heuristic`: offline tests only; it labels itself "not a Claude analysis".
- **Rules** (`config/shortlist_rules.yaml`): per-dimension reject levels, freshness reject levels, optional score and missing-skill thresholds, and "require the analyzer's recommendation". Every rule's pass/fail is stored. Excluded companies and roles from preferences are rejected before any Claude call.

## Daily run (Phase 5.5)

`backend/services/daily.py` sequences the existing services under one parent `AgentRun` (`run_type=daily`):

1. `DiscoveryService` for each enabled source, as a child discovery run. It covers fetching, normalization, dedupe, role/location matching, and freshness and exclusion diagnostics. A source that fails is recorded, and the next source still runs.
2. `select_jobs_for_daily` picks DISCOVERED/FAILED jobs that are FRESH now, plus REJECTED_BY_AGENT jobs whose latest analysis saw them as UNKNOWN and that now have a fresh post date from their own source. STALE and UNKNOWN jobs are not sent to Claude, because the shortlist rules reject them anyway.
3. `AnalysisService` with the shipped shortlist rules runs as a child analysis run.
4. The report is logged as `DAILY_RUN_COMPLETED`. It holds the counts, the per-source results, the failures, the jobs that became SHORTLISTED / READY_TO_APPLY during the run (from the timeline), and the counts of applications, browser sessions and submission attempts before and after.

**Post dates learned later.** On rediscovery, a job with no post date takes the publication timestamp that its **own** source now publishes for the same posting (source-id or URL match), logged as `JOB_POST_DATE_LEARNED`. An existing post date is never replaced, and discovery time is never used.

**Locking.** The parent holds the one-active-run lock: children are created with `check_active=False`, and manual discovery/analysis get `409` meanwhile. A heartbeat thread keeps the parent alive during long Claude batches. If the process dies, the stale-run rule closes the run as FAILED, so it does not block the next day.

**Scheduling.** `DailyScheduler` is a daemon thread started in the FastAPI lifespan when `DAILY_RUN_ENABLED`. It fires once `DAILY_RUN_TIME` (in `TIMEZONE`) has passed and no daily run has started since that day's slot. That rule also catches up after downtime. It retries after 10 minutes if another run is active, and re-checks the clock at least hourly.

## Lead -> trusted ATS resolution (Phase 5.7)

Aggregators whose timestamps PIE does not trust (Adzuna `created`, Himalayas `pubDate`) are **lead sources**. Their dates never become a `posted_at` and never make a job fresh. The rule is that leads discover boards, and boards discover jobs. The flow is off by default (`lead_resolution.enabled: false` in `config/job_sources.yaml`).

1. **Register** (`backend/services/leads.py`: `register_leads`). Adzuna rows seen in the run, and Himalayas results if enabled, become `job_leads` rows.
   - Only postings that match the configured roles and locations are registered.
   - Excluded companies are stored as REJECTED.
   - Every aggregator timestamp is stored as untrusted, and query strings are removed from URLs.
2. **Resolve the employer** (`resolve_leads`). Each company is looked up in this order:
   - the boards already in `job_sources.yaml`;
   - `lead_resolution.overrides`;
   - up to 3 board-id guesses probed on Greenhouse, SmartRecruiters, Ashby and Lever. Probes use the adapters' documented listing endpoints only (`integrations/job_sources/board_probe.py`), within a per-run request budget, stopping a platform on 429, with a 14-day negative cache.

   A board is VERIFIED only on proof of identity:
   - the Greenhouse or SmartRecruiters board name equals the company;
   - for Lever and Ashby, at least 80% of at least 3 postings name the company;
   - a board ID of 3 characters or fewer stays CANDIDATE.
3. **Crawl approved boards** (`crawl_active_boards`). Only VERIFIED and *active* boards are crawled. A board becomes active through `python -m backend.scripts.leads boards --approve`, or through the opt-in `auto_activate_verified`.
   - The platform's **existing adapter** (`registry.build_source_for_boards`) and the normal `DiscoveryService` do the crawling, so every filter applies.
   - Each posting gets its date from its own adapter.
4. **Match** (`match_leads`, using `dedupe.match_same_job`). A lead links to a posting from its board only on:
   - an explicit ATS ID or canonical URL; or
   - all of these, for exactly one posting: the same normalized title; the same city, or the same configured location when either side names no city; the ATS posting no newer than the lead's first sighting plus 6 hours; and at least 50% shingle overlap when the lead has a description. Without a description, a city match is required.

   More than one plausible posting gives AMBIGUOUS, and none is ever picked.
5. **Deduplicate.** The matched aggregator row gets `jobs.duplicate_of_id`. It becomes DUPLICATE only if it was DISCOVERED; a rejected row keeps its state. Its `posted_at` stays NULL.
   - With lead resolution on, `DiscoveryService` no longer folds a trusted posting into an aggregator row with no date (a fingerprint or company-title-city match). It stores the trusted posting as its own job and links the aggregator row when the strict rules hold.
   - Analysis selection skips rows with `duplicate_of_id`, so each real job is analyzed once.

**Resolution order (Phases 5.7B–D).** Companies are resolved in a deterministic order, and one resolution serves all of a company's leads. The order only decides who is looked up first; eligibility, freshness, verification and matching are unchanged. The sort key (`leads.resolution_queue`) is:

1. **Tier.**
   - *Free* (0 requests): a verified board, a board in `job_sources.yaml` (matched by name, or proven by stored trusted ATS jobs), an unexpired negative-cache entry, or a recent candidate.
   - An explicit override.
   - A supported-ATS board quoted in a lead: exactly one request, and identity must still be proven.
   - Never checked.
   - Re-check after the cache expired.
2. **Candidate fit** of the company's own leads (`normalize.seniority_evidence`, using the profile's verified years):
   - entry-level evidence first (Graduate, Junior, SDE I, "freshers", "0-1 years" and similar);
   - no evidence next;
   - *senior-only* last: every lead has a senior or numbered-level title, or asks for more experience than the candidate has.

   Senior words win over entry words, internships are never entry evidence, and "Associate" titles are not trusted.
3. **Cost:** untried platform × ID-guess pairs, capped at `max_requests_per_company` (8).
4. **Lead count**, bucketed 1 / 2 / 3+.
5. **Most recently seen lead** (PIE's own clock).
6. **Company name.**

Aggregator dates are not part of the key.

**Probing rules.**
- Probes are guess-major within the per-company cap.
- Each (platform, ID) is asked at most once per run.
- Definite misses (404 or an empty board) are stored on the negative-cache entry and reused for every company until it expires.
- Errors, timeouts and 429s are never misses.
- A board already recorded for another company is never reused.
- `GET /api/leads/summary` and the dashboard's *Lead resolution* card show lead and board states, boards waiting for review, the negative cache and the budgets.

The daily report gains a `leads` section and a `new_fresh_jobs` count. `fresh` counts fresh *postings*, including repeats.

## API

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | Overall status, version, `development_mode`, and per-component status (database, Claude analyzer, Gmail) |
| GET | `/api/dashboard/overview` | KPI counts, funnel, recent timeline events, latest agent run |
| GET | `/api/jobs` | Paginated jobs. Filters: `q`, `location`, `source`, `freshness`, `status`, `shortlisted`, `min_salary` (unknown salaries kept unless `salary_known_only`). Sort: `newest`, `posted`, `salary`, `match`, `company` |
| GET | `/api/jobs/filters` | Filter options (sources, top locations, freshness window) |
| GET | `/api/jobs/shortlisted` | Shortlisted jobs, best match first |
| GET | `/api/jobs/{id}` | Job detail with every analysis and the timeline |
| GET | `/api/daily-run` | Daily schedule (enabled, time, timezone, next run) and the latest daily run with its report |
| POST | `/api/daily-run` | Starts a daily discovery + analysis run now; `202`, or `409` if a run is active |
| GET | `/api/leads/summary` | Lead -> trusted ATS resolution state: leads by status, boards by status, boards waiting for review, approved boards, negative cache, budgets (read-only) |
| POST | `/api/jobs/discover` | Starts discovery in the background (`{"source": ...}` optional); returns `202` with run IDs |
| POST | `/api/jobs/{id}/analyze` | Analyzes one job synchronously and applies the rules |
| POST | `/api/jobs/analyze-shortlist` | Background batch analysis of waiting jobs (`limit`, `include_stale`, `reanalyze`) |
| GET | `/api/agent/status` | Idle, running or stopping; active run progress; recent runs; analyzer in use |
| GET | `/api/agent/runs`, `/api/agent/runs/{id}` | Run history, and one run with its action log |
| POST | `/api/agent/runs/{id}/cancel` | Asks a running run to stop |
| POST | `/api/jobs/{id}/tailored-resume` | Creates a validated tailored resume (`tailoring_strategy`, `requested_format`, `force_new`). `422` with `violations` when validation fails twice |
| GET | `/api/applications`, `/api/applications/{id}` | Applications; one application's sessions, fields, challenges, screenshots, external-action log, attempts and timeline |
| POST | `/api/applications/{id}/dry-run` | Deterministic DRY RUN (never submits) |
| POST | `/api/applications/{id}/answers` | Your answers to questions PIE could not answer (stored as verified) |
| POST | `/api/applications/{id}/live/prepare` | LIVE only (`403` unless `EXTERNAL_LIVE_ENABLED`): fill and validate, then wait |
| POST | `/api/applications/{id}/sessions/{sid}/confirm-live` | Your explicit confirmation; must echo the exact statement shown |
| POST | `/api/applications/{id}/sessions/{sid}/submit` | Live submit of a prepared, confirmed session (`428` without confirmation, `409` if already attempted) |
| POST | `/api/applications/{id}/sessions/{sid}/resolve` | You settle an unconfirmed live submission |
| POST | `/api/applications/{id}/sessions/{sid}/close` | Close the browser session |
| GET | `/api/applications/{id}/sessions/{sid}/screenshots/{n}` | A stored screenshot (only from the artifacts directory) |
| GET | `/api/resumes/master` | Whether the master resume is available (file name and SHA-256; never its content) |
| GET | `/api/resumes/versions`, `/api/resumes/versions/{id}` | Resume versions (`job_id` filter), and one version with its change summary, report and Markdown |
| GET | `/api/resumes/versions/{id}/artifact?format=pdf\|markdown` | Downloads a stored tailored artifact (served only from `resumes/tailored/`) |

Interactive docs are served at `/docs`.

## Dashboard

The dashboard pages are Next.js App Router server components that fetch from the backend using `PIE_API_URL`, a server-side variable. Browser actions (Discover, Analyze, Stop, live status polling) go through a same-origin proxy route, `app/backend/[...path]`, which only forwards PIE API paths.

If the backend is unreachable, every page says so and shows no numbers. There is no demo fallback, so the dashboard only ever reflects the database.

The theme is built from CSS tokens (light/dark/system). Every status shows a colored icon *and* a text label.

## Development mode

`DEVELOPMENT_MODE=true` is the default:

- no real submissions
- mock jobs, forms and emails
- intended actions are logged as `DRY_RUN`

`python -m backend.scripts.seed_mock` fills an empty database with clearly fictional data (`source="demo-seed"`, `*.example`/`*.invalid` domains).

Discovery is read-only, so it may use real public job boards in development mode. `--source mock` keeps it fully offline. It refuses to run when development mode is off.

## Build phases

1. **Foundation**: repo, backend, database, migrations, dashboard shell. **Done.**
2. **Discovery and analysis**: `JobSource` interface, Greenhouse and mock adapters, normalization, dedupe, freshness, runs, Claude analysis with validated structured output, configurable shortlist rules, Jobs and Shortlisted pages. **Done.**
3. **Agent core**: tool registry with permissions (`EXTERNAL_ACTION` disabled), the orchestrator loop with Claude decisions, persistent and resumable runs, structured events, internal application drafts, the explicit application state machine, and the Agent page. **Done.** See [AGENT.md](AGENT.md).
4. **Resume tailoring**: validated, versioned job-specific resumes from the read-only master, change summaries and tailoring reports, readiness gating for `READY_TO_APPLY`, Resumes pages. **Done.**
5. **Browser execution**: Playwright service, application tools behind the permission gate, DRY RUN by default, CAPTCHA/OTP/login handling, duplicate protection, confirmation evidence, the Application Execution view and a local fixture site. **Done** (live submission stays disabled until you enable it).
6. Gmail OAuth, classification, email→application matching.
7. Excel export.
8. Full dashboard integration (jobs, applications, emails, agent control, settings, logs).
9. End-to-end mock environment and tests.
10. Production automation, enabled only after phase 9 passes.
