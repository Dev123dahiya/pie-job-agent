# PIE — Personal Intelligence Engine · Job Agent

A personal job-search and application agent. Claude does the reasoning. PIE provides the tools, persistence, permissions, logs and dashboard.

> **Status: Phase 5 (browser execution, DRY RUN by default).** PIE discovers jobs from the Greenhouse public job-board API. Claude analyzes and shortlists them against your verified profile. An autonomous Claude agent can now work through PIE's registered tools: reading your pipeline, analyzing jobs, creating validated job-specific resumes from your master resume, preparing application drafts and running applications in a real browser as a **DRY RUN** (everything except the final submit). **Live submission is disabled by default** (`EXTERNAL_LIVE_ENABLED=false`) and, when enabled, still needs your explicit confirmation for each application. No email is sent. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/AGENT.md](docs/AGENT.md).

## Stack

- **Backend:** FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL
- **Dashboard:** Next.js 16, React 19, TypeScript, Tailwind CSS 4
- **Job analysis:** Claude, through the Anthropic API or your local Claude Code CLI
- **Later phases:** Playwright (browser), Gmail API, XLSX export
- **Tests:** pytest, Vitest and Testing Library

## Prerequisites

- Python 3.11+ (developed on 3.14)
- Node.js 20+ (developed on 24)
- PostgreSQL 15+, either through Docker (`docker compose`) or a local install

## Setup

```bash
# 1. Configuration
cp .env.example .env
cp config/user_profile.example.yaml config/user_profile.yaml   # then edit with your real details
cp dashboard/.env.example dashboard/.env.local

# 2. Database (PostgreSQL in Docker)
docker compose up -d postgres

# 3. Backend
python -m venv .venv
.venv/Scripts/activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
alembic upgrade head
python -m backend.scripts.seed_mock      # optional: fictional demo data (dev mode only)
python -m backend.serve --reload       # http://127.0.0.1:8000 (this computer only)

# 4. Dashboard (new terminal)
cd dashboard
npm install
npm run dev                       # http://127.0.0.1:3000 (this computer only)
```

## Discover and shortlist jobs

1. Configure the search in `config/job_sources.yaml`: roles, locations and which company boards to search.
2. Configure the shortlist rules in `config/shortlist_rules.yaml`.
3. Put your real facts in `config/user_profile.yaml`. Keep `verified_profile` (facts) and `preferences` (wishes) separate.

```bash
python -m backend.scripts.jobs discover                  # all enabled sources (Greenhouse)
python -m backend.scripts.jobs discover --source mock    # offline fixtures, no network
python -m backend.scripts.jobs analyze --limit 10        # Claude analysis + shortlist rules
python -m backend.scripts.jobs runs                      # recent runs and their counts
```

The dashboard's **Jobs** page runs the same steps with **Discover jobs** and **Analyze & shortlist**, and shows live progress.

**Claude backend.** With `ANALYZER_BACKEND=auto`, analysis uses the Anthropic API when `ANTHROPIC_API_KEY` is set. Otherwise it uses the Claude Code CLI (`claude`), signed in with your Claude subscription. Every analysis stores which analyzer and model produced it. The `heuristic` backend exists for offline tests and is always labelled "not Claude".

**Stale jobs.** By default, batch analysis skips stale jobs (older than `maximum_application_age_hours`) to save Claude calls. Jobs with no posting date are *unknown*, not stale, and are analyzed.

The API is served at http://127.0.0.1:8000, with docs at `/docs` and the health check at `/api/health`.

If the backend isn't running, the dashboard still loads and shows **"Backend offline"**. It never substitutes demo numbers.

## Daily discovery and analysis

Once a day, PIE runs discovery on every enabled source, then Claude analysis of the **fresh** jobs, then the shortlist rules. It never creates or submits applications and never opens a browser.

- **When.** Every day at `DAILY_RUN_TIME` (default `07:00`, `TIMEZONE` default `Asia/Kolkata`), while the API (`python -m backend.serve`) is running. If the machine was off at that time, the run happens on the next start that day. A day whose slot was already used, by a scheduled or a manual run, is not run again.
- **Run it now.** Use `python -m backend.scripts.daily run` (add `--json` for the full report), or `POST /api/daily-run`. `python -m backend.scripts.daily status` shows the schedule and the latest report.
- **Turn it off.** Set `DAILY_RUN_ENABLED=false` in `.env` and restart the API. Manual runs still work.
- **Without the API running.** You can schedule the CLI with Windows Task Scheduler instead, for example `schtasks /Create /SC DAILY /ST 07:00 /TN "PIE daily run" /TR "<repo>\.venv\Scripts\python.exe -m backend.scripts.daily run"`. PIE does not register this for you.
- **Idempotent.** Jobs are deduplicated. Only never-analyzed (or failed) FRESH jobs go to Claude. STALE and UNKNOWN jobs are ineligible, so they are not sent. A stale job never becomes fresh by being rediscovered. An UNKNOWN job is reconsidered only if its own source later publishes a post date.
- **History.** Each run is an agent run of type `daily`, with one child discovery run per source and one analysis run. Its report (counts, per-source results, failures, newly shortlisted / READY_TO_APPLY jobs) is on the dashboard overview and at `GET /api/daily-run`.

## Aggregator leads (off by default)

Adzuna and Himalayas timestamps are never trusted. With `lead_resolution.enabled: true` in `config/job_sources.yaml`, each daily run turns their postings into *leads* and looks for the employer's board on Greenhouse, SmartRecruiters, Ashby or Lever. Review what it finds with `python -m backend.scripts.leads list` and `python -m backend.scripts.leads boards`.

Employers whose own leads show entry-level evidence are looked up first, and employers whose leads are all senior are looked up last. The dashboard's *Lead resolution* card lists the boards waiting for your review. Once you approve a board (`boards --approve greenhouse:<board>`), PIE reads that board with the normal adapter. Its postings, with the ATS's own publication dates, go through the unchanged freshness, role, location, experience and salary rules and are analyzed once. A lead is never a job and never makes anything fresh.

## Autonomous agent

On the dashboard's **Agent** page, pick a preset or write a task, choose **Read only** or **Internal writes**, set a step limit and press **Start agent**.

You can watch each step (summaries only), stop the run, and answer when it pauses with **Needs your input**. The same is available from the command line:

```bash
python -m backend.scripts.agent run "Review my job pipeline" --mode read_only
python -m backend.scripts.agent show <run_id>
python -m backend.scripts.agent resume <run_id> --message "..."
```

Claude acts only through registered, schema-validated, permission-checked tools. `EXTERNAL_ACTION` tools (submit, email, websites) are hard-disabled in this phase.

To let the agent read your master resume, set `MASTER_RESUME_PATH` in `.env` (it is only ever read, never modified), or copy the file into `resumes/master/`. Details are in [docs/AGENT.md](docs/AGENT.md).

## Job sources

Discovery reads only official, public job APIs that companies (or job boards) publish for exactly this purpose. There is no scraping, no login and no CAPTCHA/rate-limit workaround. Every source identifies itself with PIE's User-Agent, waits between requests, stops for the run on HTTP 429, and treats 401/403 as "not public".

| Source | Method | Post date used | Salary |
|---|---|---|---|
| Greenhouse | Job Board API `GET /v1/boards/{board}/jobs` | `first_published` (published) | pay ranges when published |
| Lever | Postings API `GET /v0/postings/{company}?mode=json` (paginated) | `createdAt` (created: publication is on or after it, so age is never understated) | `salaryRange`, yearly or monthly only |
| Ashby | Job Board API `GET /posting-api/job-board/{board}` | `publishedAt` (published) | one explicit yearly/monthly salary component only |
| SmartRecruiters | Posting API `GET /v1/companies/{company}/postings?country=in` (paginated) + detail | `releasedDate` (released) | not published by the API: unknown |
| Remotive | public API `GET /api/remote-jobs?category=` (at most 2 requests per run, per its terms) | `publication_date`, which has no time zone, so it is stored as the oldest instant it could be | free text kept as stated, never parsed |
| Adzuna (off until you add a key) | Jobs API `GET /v1/api/jobs/in/search/{page}?what=&where=&max_days_old=2` with **your** free key (`ADZUNA_APP_ID`/`ADZUNA_APP_KEY` in `.env`); 8 roles × Gurugram/Noida/Delhi, ≤2 pages, 3 s between requests, ≤60 requests per run (terms: 25/min, 250/day) | none by default: Adzuna does not document what `created` means, so freshness stays UNKNOWN (opt-in `created_is_publication: true` only if you verify it) | numeric figures have no documented period and estimates (`salary_is_predicted`) are ignored: UNKNOWN, raw value kept |

- **Freshness** is `now - post date <= 48 hours`. A source that gives only an update time gets no post date (freshness UNKNOWN), and PIE never presents an update as a new posting. The kind of timestamp is stored per job (`posted_at_kind`) and shown on the Jobs page.
- **Salary** is annualized only from yearly or monthly figures (monthly ×12). Hourly or unclear figures, and anything not disclosed, stay UNKNOWN; the source's original value is kept in `salary_raw`.
- **Duplicates** are detected by source id, canonical URL, or company + normalized title + location. The same posting on another source is stored once, and every source is kept in `source_attributions` (shown as "also on ...").
- **Early-career titles** ("Graduate Engineer", "Associate Engineer", "Engineer I", "Junior Developer", ...) are search aliases of the 8 configured roles. A title match only means "analyze it": Claude's analysis and the shortlist rules decide fit, experience, salary and freshness, and those rules are unchanged.

## Resume tailoring

On a job's page, the **Resume & readiness** card shows whether the master resume is available, the latest tailored version, its change summary, missing information and draft readiness. **Create Tailored Resume** (internal only, nothing is submitted) asks Claude for a job-specific version of your master resume:

- The master resume is only ever read. Each run creates a **new** version (`resumes/tailored/job-<id>/resume_<id>_v<n>.pdf` and `.md`); existing files are never overwritten.
- Every claim is validated against the master resume and your verified profile. Unsupported skills, technologies, employers, titles, dates, projects, education, certifications, publications or metrics are rejected. Unconfirmed skills (for example Go) are never allowed. A rejected version is regenerated once with the violations as feedback; if it still fails, nothing is saved.
- Any experience gap (the job's required experience vs your verified full-time experience; internships are not counted) is recorded in the report and shown on the job and resume pages. It is never hidden or softened.
- An identical request (same job, master, profile, strategy and format) reuses the existing version unless you ask for a new one.
- A job becomes `READY_TO_APPLY` only with the master resume, a validated tailored resume, an internal draft and all standard answers. **Submission is not enabled.**

The **Resumes** page lists every version, with its change summary, tailoring report (matches and gaps, no ATS score), validation checks and downloads. See [docs/AGENT.md](docs/AGENT.md#resume-tailoring-phase-4).

## Browser execution (dry run by default)

Open a `READY_TO_APPLY` application under **Applications** and press **Run DRY RUN**. PIE opens the stored application page with Playwright, detects the form, maps each field to a **verified** answer, fills it, uploads the validated tailored resume, validates the form and **stops before the final submit**. The page shows the banner "DRY RUN — Nothing has been submitted." In a dry run the browser also aborts every outgoing non-GET request, so nothing can be sent even by accident.

- **Unknown questions** (e.g. night shifts, visa sponsorship, work authorization for another country), **consent checkboxes** and **demographic questions** are never answered by PIE. The job becomes `BLOCKED_NEEDS_USER` and the page asks you. Your answers are saved as verified (optionally for every application) and the run can be resumed.
- **CAPTCHA, OTP, SMS/email verification, 2FA, identity checks, login walls and bot blocks** stop the session. PIE never solves, bypasses or guesses them.
- **Only `VERIFIED_AUTOFILL` fields are filled.** Every question is classified as `VERIFIED_AUTOFILL`, `UNKNOWN`, `AMBIGUOUS`, `SENSITIVE_USER_REQUIRED` or `CHALLENGE`. Required fields in the other categories stop for you; optional ones are skipped.
- **Verified answers** are exactly what you confirmed in `verified_answers` (for example work authorization, relocation, notice period and expected salary), plus your identity, contact details and links. Nothing is stored for sponsorship, citizenship, demographics, security or other countries' legal questions.
- **First/last name** come from your verified identity: `first_name`/`last_name` in `verified_profile`, or a name of exactly two words. A three-word name ("Asha Rani Verma") is not split. If two sources disagree, the field is `AMBIGUOUS`.
- **Dropdowns** (native selects and react-select style comboboxes, including searchable ones) are opened, searched and set only when exactly one option clearly matches the verified answer. The shown value is then checked. There is no "closest match".
- **Uploads in a dry run** are reported honestly: `UPLOAD_UI_SUCCESS` (the file is in the form, nothing was sent), `UPLOAD_REQUEST_BLOCKED_BY_DRY_RUN_GUARD` (the site tried to send it and the guard stopped it, so the employer did not receive it) or `UPLOAD_FAILED`. `UPLOAD_SUCCESS` only exists in live runs.
- **Headless vs visible.** A dry run may be headless. Any session that could need you (CAPTCHA, OTP, 2FA, identity check, login) needs `BROWSER_HEADLESS=false`. A paused page stays open while the backend runs. After a restart, resuming reloads the page and rebuilds the recorded form state.
- **No real dry run yet.** None of the current real jobs is `READY_TO_APPLY`. A real dry run should happen only once a genuinely suitable job gets there; nothing is shortlisted artificially.
- The agent can do the same in the `external_dry_run` mode on the Agent page.

**Live submission** is blocked by default. All of these must hold:

- `DEVELOPMENT_MODE=false`. While `DEVELOPMENT_MODE=true`, live is refused with `DEVELOPMENT_MODE_BLOCKED` even if `EXTERNAL_LIVE_ENABLED=true`.
- `EXTERNAL_LIVE_ENABLED=true`. It is off by default and never switched on by code.
- `BROWSER_HEADLESS=false`, so you can handle a CAPTCHA, OTP or login yourself (otherwise `VISIBLE_BROWSER_REQUIRED`).

Every page shows the current mode before anything runs: **DRY RUN / DEVELOPMENT MODE / LIVE DISABLED**, or a red **LIVE EXTERNAL ACTIONS ENABLED**. Even with live enabled, each application needs:
1. a prepared session (**Prepare LIVE submission**);
2. your explicit in-page confirmation of "You are about to submit an application to <company> for <job>. This will perform an external action.";
3. a positive confirmation on the employer's page before PIE records `SUBMITTED`.

PIE never submits twice. An unclear result blocks for you ("I confirmed it was submitted" / "It was not submitted") and is never retried. Details are in [docs/AGENT.md](docs/AGENT.md#browser-execution-phase-5).

Playwright uses its bundled Chromium if installed (`python -m playwright install chromium`), otherwise your installed Microsoft Edge or Google Chrome.

## Checks

```bash
# Python
pytest                  # unit + integration tests (SQLite, no external services)
# Also verify migrations on real PostgreSQL (uses a disposable, empty database):
#   docker compose exec postgres psql -U pie -d postgres -c "CREATE DATABASE pie_test;"
#   PIE_TEST_POSTGRES_URL=postgresql+psycopg://pie:pie@localhost:5432/pie_test pytest
ruff check . && ruff format --check .
mypy

# Dashboard
cd dashboard
npm run lint
npm run typecheck
npm test
npm run build
```

## Troubleshooting

- **`docker compose up` fails with `error getting credentials`:** the Docker Desktop credential helper is failing. Signing in to Docker Desktop usually fixes it. Alternatively, pull once with an empty config: `DOCKER_CONFIG=$(mktemp -d) docker pull postgres:17-alpine`.
- **Analysis returns 503 "No Claude analyzer available":** set `ANTHROPIC_API_KEY`, or install the Claude Code CLI and run `claude` once to sign in.
- **Dashboard shows "Backend offline":** the backend isn't reachable at `PIE_API_URL` (default `http://127.0.0.1:8000`).

## Safety defaults

- External work defaults to **DRY RUN**. Live submission needs `EXTERNAL_LIVE_ENABLED=true` (default `false`) plus your confirmation for every application. `DEVELOPMENT_MODE` does not itself gate browser execution; the live switch does.
- The agent never fabricates information and never bypasses CAPTCHA, OTP, MFA, identity checks or access controls. Blocked applications are paused for the user.
- No passwords, cookies or session tokens are stored or logged. URLs are redacted and screenshots mask password and one-time-code fields (`data/applications/`, git-ignored).
- Secrets live in `.env`, which is git-ignored. So are `config/user_profile.yaml`, resumes, logs, exports and any OAuth tokens or cookies.

## Layout

```
backend/        FastAPI app, settings, profile loader, services, scripts
database/       models, enums, repositories, state machine, Alembic migrations
agent/          Claude client, analysis, agent decision + prompt, tool registry (agent/tools)
browser/        Playwright tools (Phase 5)
integrations/   job_sources (P2), gmail (P6), excel (P7)
config/         user_profile.example.yaml, job_sources.yaml, shortlist_rules.yaml
resumes/        master/ (source of truth), tailored/
data/           logs/, exports/
dashboard/      Next.js app
tests/          pytest suites
docs/           architecture
```
