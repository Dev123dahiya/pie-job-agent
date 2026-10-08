# PIE Job Agent: autonomous agent (Phases 3-5)

> **External application submission is NOT enabled.** In Phase 3 the agent can read PIE's data and
> change PIE's own internal state (analyses, internal drafts, statuses, notes, local exports). It
> cannot submit applications, send email or use websites. Those tools exist only as disabled
> `EXTERNAL_ACTION` entries.

## How a run works

```
USER --(task)--> Dashboard /agent  or  python -m backend.scripts.agent run "..."
        |
        v
PIE AGENT ORCHESTRATOR   backend/services/agent_runner.py   (AgentRun + AgentAction in the DB)
        |   each step: build context -> ask Claude -> validate -> permission-check -> run tool -> record
        v
CLAUDE (decision only)   agent/claude_client.py + agent/decision.py + agent/agent_prompt.py
        |   returns ONE JSON decision: call_tool | complete | need_user_input
        v
TOOL REGISTRY            agent/tools/   (the only way Claude can affect anything)
```

Each step, Claude receives:
- **TASK**
- **VERIFIED_PROFILE**: confirmed facts only. It has no contact details and no unconfirmed skills.
- **UNCONFIRMED**: a list of skills that must never be used.
- **PREFERENCES**
- **CURRENT_AGENT_STATE**: the iteration budget, the mode, the allowed permissions, and `external_actions_enabled: false`.
- **AVAILABLE_TOOLS**: name, description, permission, enabled flag and input schema for each tool.
- **HISTORY**: this run's previous decisions and tool results, rebuilt from the database.

Claude replies with structured JSON:
```json
{"summary": "one line shown to the user", "action": "call_tool",
 "tool": "get_job", "arguments_json": "{\"job_id\": \"...\"}", "message": ""}
```

**Validation.** The reply is checked with Pydantic (`AgentDecision`: extra keys forbidden, consistent fields, arguments must be a JSON object). A malformed reply is rejected and Claude is re-asked with the error (up to 2 times).

**What's stored.** Only the one-line `summary` and the tool inputs and outputs are kept. There is no hidden chain of thought: the CLI backend does not return one, and the API backend doesn't request or store one.

**Backends.** The agent uses the same Claude integration as job analysis (`agent/claude_client.py`):
- **Anthropic API**, when `ANTHROPIC_API_KEY` is set. It uses structured outputs and the server-side refusal fallback.
- **Claude Code CLI**, which uses your subscription. It runs as `claude -p --json-schema` with no tools, no MCP servers and no settings or hooks, in a neutral working directory.

In `ANALYZER_BACKEND=heuristic` (test) mode the agent refuses to run rather than calling Claude.

## Stopping conditions

| Condition | Run status | Event |
|---|---|---|
| Claude chooses `complete` | `COMPLETED` (the message becomes the run summary) | `RUN_COMPLETED` |
| Claude chooses `need_user_input` | `NEEDS_USER_INPUT` (`pending_user_input` holds the question) | `USER_INPUT_REQUIRED` |
| User presses Stop / `POST .../cancel` | `STOPPED` (checked before each decision and before each tool) | `RUN_CANCELLED` |
| Iteration limit (1–40, default 12) | `FAILED` ("Iteration limit reached"), resumable with `extra_iterations` | `RUN_FAILED` |
| Wall-clock limit (30 min) | `FAILED` | `RUN_FAILED` |
| More than 2 malformed decisions, more than 2 transient Claude errors, or Claude unavailable | `FAILED` | `RUN_FAILED` |
| The same tool call repeated 5 times | `FAILED` ("avoid a loop") | `RUN_FAILED` |
| Worker crash / backend restart | `FAILED` (a heartbeat older than 10 minutes is reaped); resumable | `RUN_FAILED` |

**Resuming.** `POST /api/agent/runs/{id}/resume` with `{message?, extra_iterations?}` continues a paused, failed or stopped run. The history is rebuilt from stored events, so the database, not memory, is the source of truth. The user's reply is recorded as a `USER_MESSAGE` event.

**Events.** `RUN_STARTED`, `RUN_RESUMED`, `USER_MESSAGE`, `THINKING` (the step summary), `TOOL_CALL_STARTED`, `TOOL_CALL_COMPLETED`, `TOOL_CALL_FAILED`, `STATE_CHANGED`, `USER_INPUT_REQUIRED`, `RUN_COMPLETED`, `RUN_FAILED` and `RUN_CANCELLED`, stored in `agent_actions`. The `tool`, `iteration` and `idempotency_key` columns are set for tool calls.

## Tools and permissions

| Permission | Meaning | Allowed in |
|---|---|---|
| `READ_ONLY` | Reads PIE state | `read_only` and `internal_writes` runs |
| `WRITE` | Changes PIE's own database or local files only | `internal_writes` runs |
| `EXTERNAL_ACTION` | Drives an employer's website in a browser | `external_dry_run` and `external_live` runs only. `external_live` also needs `EXTERNAL_LIVE_ENABLED=true` |

| Tool | Permission | What it really does |
|---|---|---|
| `get_user_profile` | READ_ONLY | Verified facts, unconfirmed skills (flagged) and preferences; no contact details |
| `get_application_answers` | READ_ONLY | Which answers exist (answer keys, VERIFIED or UNCONFIRMED, and their source; never the values), plus the common questions with no verified answer. Keys are the ones `fill_application_field` accepts |
| `search_jobs` | READ_ONLY | Queries PIE's stored jobs (no web browsing or discovery) |
| `get_job` | READ_ONLY | Posting facts, the latest analysis with failed rules, and any draft |
| `get_shortlisted_jobs` | READ_ONLY | Jobs in `SHORTLISTED` |
| `get_master_resume` | READ_ONLY | Text of the master resume (`MASTER_RESUME_PATH` or `resumes/master/`). Never modified |
| `list_resume_versions` | READ_ONLY | Resume versions recorded in PIE (optionally for one job) |
| `get_resume_version` | READ_ONLY | One version with its change summary, tailoring report and validation report |
| `get_application` | READ_ONLY | One application, with its blockers and timeline |
| `get_agent_status` / `get_current_run` | READ_ONLY | Agent and run status |
| `get_application_statistics` | READ_ONLY | Counts by job status and application status |
| `analyze_job` | WRITE | Runs the Phase 2 Claude analysis and shortlist rules on one job |
| `create_application_draft` | WRITE | Creates an **internal** draft for a `SHORTLISTED` job (idempotent) and links the latest validated tailored resume. The job becomes `READY_TO_APPLY` only if nothing is missing, `BLOCKED_NEEDS_USER` with the exact missing items, or otherwise stays `SHORTLISTED` with the pending steps |
| `update_application_status` | WRITE | Moves the job to `READY_TO_APPLY` (only if nothing is missing), `BLOCKED_NEEDS_USER` (must say what the user must do) or `WITHDRAWN`. It refuses `APPLICATION_IN_PROGRESS` and `SUBMITTED` |
| `record_agent_action` | WRITE | Adds a note to the run's audit log |
| `export_application_data` | WRITE | Writes a CSV or JSON file to `data/exports/` |
| `create_tailored_resume` | WRITE | Creates a **new** validated tailored resume version from the master resume (see below). Internal only |
| `open_application` | EXTERNAL_ACTION | Opens a `READY_TO_APPLY` job's stored application URL. It checks the draft, the validated resume and duplicates first. It resumes a paused session and stops on challenges |
| `inspect_application_form` | EXTERNAL_ACTION | Detects the fields and maps each to a verified answer key. Unknown required questions pause for the user |
| `fill_application_field` | EXTERNAL_ACTION | Fills verified answers (`use_mapping`, or `[{field_id, answer_key}]` for unrecognised text fields and contact keys only). Claude never supplies values |
| `upload_application_resume` | EXTERNAL_ACTION | Uploads the application's validated tailored resume PDF |
| `save_application_progress` | WRITE | Records state and a screenshot in PIE (nothing is clicked) |
| `prepare_submission` | EXTERNAL_ACTION | Validates the form and stops before submit. This is the last step of a dry run |
| `submit_application` | EXTERNAL_ACTION | **Irreversible.** LIVE session only, after the user's confirmation, never twice. A dry run returns `DRY_RUN_ONLY` |
| `verify_submission` | EXTERNAL_ACTION | Re-checks the page for positive confirmation of a live attempt |
| `close_application_session` | WRITE | Closes the browser. With nothing submitted, the job returns to `READY_TO_APPLY` |
| `get_application_session` | READ_ONLY | A session's stored state |
| `send_email` | EXTERNAL_ACTION | **Not enabled** (`NOT_ENABLED`); email is a later phase |

**Every tool call** goes through the registry (`agent/tools/base.py`):
1. The tool is looked up by name.
2. The arguments are validated against its Pydantic input schema (extra arguments are an error).
3. The permission gate runs.
4. The tool executes inside a database savepoint, so a failed tool rolls back its writes.
5. The output is validated against its schema.
6. A structured `ToolResult` is returned, with an error code: `UNKNOWN_TOOL`, `INVALID_INPUT`, `PERMISSION_DENIED`, `NOT_ENABLED`, `EXTERNAL_NOT_ALLOWED`, `LIVE_NOT_ENABLED`, `DRY_RUN_ONLY`, `USER_CONFIRMATION_REQUIRED`, `DUPLICATE_APPLICATION`, `MODE_MISMATCH`, `UNEXPECTED_PAGE`, `BROWSER_ERROR`, `NOT_IMPLEMENTED_YET`, `NOT_FOUND`, `INVALID_TRANSITION`, `PRECONDITION_FAILED`, `EXECUTION_FAILED` or `VALIDATION_FAILED`.

Errors go back to Claude, which adapts.

**Retries and duplicates:**
- A read-only tool marked retryable is retried once.
- Write tools are never auto-retried.
- An identical write that already succeeded in this run is skipped (`SKIPPED`, "duplicate write skipped").
- After 2 identical calls, the third returns a warning instead of executing; the fifth stops the run.

## Resume tailoring (Phase 4)

Workflow the agent follows (prompt `agent-v2`): analyze → only if `SHORTLISTED` → `create_tailored_resume` → `create_application_draft` → **stop at `READY_TO_APPLY`**.

`create_tailored_resume(job_id, application_id?, tailoring_strategy, requested_format, force_new)`:

- Strategies: `balanced`, `skills_first`, `projects_first`, `experience_first`. Formats: `pdf` (a Markdown copy is always stored too) or `markdown`.
- Sources of truth: the master resume text and the verified profile. Job requirements are never evidence. Claude may reorder, select and reword; it may not add skills, technologies, employers, titles, dates, education, certifications, projects, achievements or metrics.
- Validation codes: `UNCONFIRMED_SKILL` (for example Go), `UNSUPPORTED_SKILL`, `UNSUPPORTED_TECHNOLOGY`, `UNSUPPORTED_EMPLOYER`, `DUPLICATE_ROLE`, `UNSUPPORTED_TITLE`, `UNSUPPORTED_DATES`, `UNSUPPORTED_PROJECT`, `UNSUPPORTED_EDUCATION`, `UNSUPPORTED_CERTIFICATION`, `UNSUPPORTED_PUBLICATION`, `UNSUPPORTED_METRIC` and `UNSUPPORTED_EXPERIENCE` (a "N years of experience" claim above the verified full-time total). There is one regeneration with the violations as feedback. A second failure returns `VALIDATION_FAILED`, saves no version or file, and records a `resume_tailoring_rejected` event.
- The output has a deterministic change summary (what was reordered, reworded or left out, rejected claims, `facts_preserved`) and a tailoring report (matching verified skills, experience and projects; gaps marked as not claimed). No ATS score is estimated.
- Duplicate protection: the same job, master SHA-256, profile SHA-256, strategy, format and prompt version return the existing version (`created: false`) unless `force_new`.
- **Experience gap** (`agent/tailoring/experience.py`): the job's stated requirement (`experience_required`, else an explicit "N years ... experience" requirement line) is compared with `verified_profile.total_experience_years`, which is verified full-time experience. Internships are listed but never counted as full-time. The result, `{required, candidate, gap: true | false | "unknown", status: gap | meets | exceeds | unknown, shortfall_years, internships, ...}`, is stored in the tailoring report as `experience_gap`, heads `missing_requirements` when there is a gap, and is returned by `GET /api/jobs/{id}` and the `get_job` tool. It is a factual constraint: tailoring improves relevance of documented qualifications only, and shortlist eligibility is decided separately by the analysis rules.
- A rejected (not shortlisted) job can still be tailored for review, but readiness stays `eligible: false` and the job state is unchanged.

## Browser execution (Phase 5)

**Development-mode safety gate.** `backend.core.config.live_execution_block` decides whether live is allowed:

| DEVELOPMENT_MODE | EXTERNAL_LIVE_ENABLED | Live |
|---|---|---|
| true | any | refused: `DEVELOPMENT_MODE_BLOCKED` |
| false | false | refused: `LIVE_NOT_ENABLED` |
| false | true | allowed, if `BROWSER_HEADLESS=false` (else `VISIBLE_BROWSER_REQUIRED`) and the user confirms each submission |

The check runs in the registry gate, when an agent run starts, resumes or loops, in every service entry point (`open_application`, `run_execution`, `submit`, `confirm_live_submission`) and in the API. It always runs before any browser code.

**Modes.** A run's `mode` is one of `read_only`, `internal_writes`, `external_dry_run` or `external_live`.
- Starting or resuming an `external_live` run is refused (`403`) unless `EXTERNAL_LIVE_ENABLED=true`.
- A dry-run session is never upgraded to live (`MODE_MISMATCH`); close it and start a new one.

**Workflow (prompt `agent-v3`).** `open_application -> inspect_application_form -> fill_application_field -> upload_application_resume -> prepare_submission`.
- In a dry run, the agent then calls `close_application_session` and completes.
- In a live session, `submit_application` works only after the user confirmed that specific prepared submission in the dashboard. A new prepare invalidates an earlier confirmation, and no tool can confirm.

**No arbitrary browser control.**
- The inputs are job ids, session ids, field ids (pattern `f<frame>_<n>`) and answer keys. No tool accepts a URL, selector, script, file path or value; a test enforces this.
- The page script is a constant in `integrations/browser/dom.py`.
- There is no `eval`, shell or JavaScript tool.

**Question classification.** `agent/execution/mapping.py` puts every field into exactly one category. Only `VERIFIED_AUTOFILL` is filled automatically, and `fill_application_field` cannot re-map a field to another answer.

| Category | Meaning | Required field | Optional field |
|---|---|---|---|
| `VERIFIED_AUTOFILL` | a verified answer (or the validated resume) fits unambiguously | filled | filled |
| `UNKNOWN` | no verified answer (or only an unconfirmed one) | blocks | skipped |
| `AMBIGUOUS` | an answer exists but maps to no single value/option, or sources conflict | blocks | skipped |
| `SENSITIVE_USER_REQUIRED` | sponsorship, citizenship, demographics, consent, security, other countries' legal questions | blocks (unless you answered it yourself) | skipped |
| `CHALLENGE` | password / one-time code | blocks | blocks |

**Dropdowns.** Custom comboboxes (`role=combobox`, `aria-expanded`, `aria-controls`, `aria-haspopup=listbox`) are detected together with their current value.
- Filling opens the dropdown, types the verified value if it is searchable, and selects an option only if `choose_option` finds exactly one clear match: the same text, or the single yes/no option for a yes/no answer.
- It then checks that the dropdown really shows that value.
- No clear match means `AMBIGUOUS`, `BLOCKED_NEEDS_USER`, and nothing selected. A value that is already selected is kept.

**Challenge detection.** Text patterns are matched only against the page's *interactive* text: labels, buttons, dialogs and forms around visible controls. A job description mentioning "2FA", "OTP" or "captcha" does not block. The whole page text is used only for short interstitial pages, such as a "checking your browser" screen. Widgets, challenge iframes, one-time-code inputs, password fields and HTTP 401/403/429 always count.

**Recovery.** A paused page is kept open while the backend runs, so resuming keeps a solved CAPTCHA or login. After a crash or restart the page no longer exists: the next step reloads the application page, re-classifies it, re-enters the same verified answers and sets the resume again. The step is recorded ("restored N/M fields").

**Uploads.** The resume upload reports `UPLOAD_UI_SUCCESS` or `UPLOAD_REQUEST_BLOCKED_BY_DRY_RUN_GUARD` in a dry run, `UPLOAD_SUCCESS` in a live run, and `UPLOAD_FAILED` when the page rejects the file. The dry-run guard is never weakened to make a dry run look successful.

**Answers.** Only `VERIFIED` answers are typed.
- Sources: the verified profile, `verified_answers`, and answers the user gave in the dashboard.
- `UNCONFIRMED` (e.g. first/last name when only the full name is verified) and `UNKNOWN` answers block.
- Consequential questions are matched strictly:
  - sponsorship is separate from work authorization;
  - authorization for another country, relocation to a non-Indian place, current salary and start date are all different questions;
  - numeric salary fields are never converted.
- Consent checkboxes and demographic questions are never answered by PIE.

**Pausing.** A tool that returns `blocked_needs_user` pauses the run deterministically (`NEEDS_USER_INPUT`); Claude is not asked again.
- The session records the challenge type, domain and what the user must do.
- After the user acts (answers questions, completes a verification), resuming reopens the page and continues.

**Duplicates and idempotency.** Before opening and again before submitting, PIE checks:
- earlier attempts for the application;
- submitted jobs with the same canonical URL, application URL, external id, or same company and title.

Submitting then runs five checks in order: database, job state, browser state, existing evidence, user confirmation. After that it commits a `submission_attempts` row with a unique idempotency key before clicking. A crash therefore leaves a `PENDING` attempt that blocks any retry until the user resolves it.

**Confirmation.** `SUBMITTED` needs positive evidence on the page: a success message and/or an application id, stored with a screenshot. A click, HTTP 200, a navigation or the absence of errors is not enough.
- `AMBIGUOUS` or employer-error results block for the user.
- The user resolves them in the dashboard ("I confirmed it was submitted" / "It was not submitted").

**Audit.** Every external action, including gate refusals, is written to `external_action_logs` with the run, application, tool, time, domain, summary, result and error. No passwords, cookies or tokens are stored; URLs are redacted, and screenshots mask password and one-time-code inputs.

## Application state machine

```
DISCOVERED -> ANALYZED -> SHORTLISTED -> READY_TO_APPLY -> APPLICATION_IN_PROGRESS -> SUBMITTED
                  \-> REJECTED_BY_AGENT        \-> BLOCKED_NEEDS_USER <-/
```

- `FAILED`, `DUPLICATE` and `WITHDRAWN` complete the set. Rules live in `database/state_machine.py`.
- Skipping a step, such as `ANALYZED → SUBMITTED` or `READY_TO_APPLY → SUBMITTED`, is rejected.
- `BLOCKED_NEEDS_USER` covers every case only the user can resolve: missing facts, CAPTCHA, OTP/2FA, login. The specific reason is stored on the application (`user_action_required`) and in the timeline.
- Only the browser execution service sets `APPLICATION_IN_PROGRESS` and `READY_FOR_SUBMISSION`. `SUBMITTED` is set only on a positively confirmed live submission, or on the user's own resolution of an unconfirmed one. `update_application_status` refuses all three.

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/agent/runs` | `{task, mode: read_only \| internal_writes, max_iterations}` → `202`. `409` if another run is active; `503` if no Claude backend is available |
| GET | `/api/agent/status` | Idle, running or stopping; the active run; the run awaiting the user; recent runs; `external_actions_enabled: false` |
| GET | `/api/agent/runs?run_type=agent` | Run history |
| GET | `/api/agent/runs/{id}` | A run with its events |
| POST | `/api/agent/runs/{id}/cancel` | A running run stops at its next safe point; a paused run stops immediately |
| POST | `/api/agent/runs/{id}/resume` | `{message?, extra_iterations?}` |
| GET | `/api/agent/tools` | Read-only tool catalog. **No** endpoint executes arbitrary tools |
| POST | `/api/jobs/{id}/application-draft` | The dashboard's "Create application draft" button, using the same registered tool, validation and rules as the agent |

The API has no authentication. It is meant for the user's own machine: bind it to localhost and don't expose it.

## CLI

```bash
python -m backend.scripts.agent run "Review my job pipeline" --mode read_only --max-iterations 8
python -m backend.scripts.agent show <run_id>
python -m backend.scripts.agent resume <run_id> --message "Yes, authorized to work in India"
python -m backend.scripts.agent cancel <run_id>
```
