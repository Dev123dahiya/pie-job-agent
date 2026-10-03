# Progress

Where the work stands, so any new session can catch up. `docs/REQUIREMENTS.md` is the source of
truth; `docs/PHASE_PLAN.md` lists agreed items pinned to later steps. Update this file at the end
of every step. No personal data here.

## Status

| Phase | Status |
|---|---|
| A: git setup + privacy cleanup | Done (merged to `main`) |
| B: bug fixes + SQLite default + requirements.txt | In progress (branch `phase-b-fixes`) |
| Dashboard (pages for existing data) | Done (D1-D5) |
| C: Naukri | Not started |
| D: LinkedIn Easy Apply | Not started |
| E: LLM answers, model split, auto mode | Not started |
| F: Gmail, Emails/Logs/Settings pages, analytics | Not started |

### Phase B steps

| Step | What | Status |
|---|---|---|
| B1 | SQLite is the default database | Done |
| B2 | Move existing data to SQLite | Done (decision only: start fresh, see below) |
| B3 | Failed browser runs are cleaned up, not stuck "in progress" | Done (approved) |
| B4 | Failed jobs are never re-scored automatically | Done (approved) |
| B5 | A resume linked to an in-progress or submitted application never changes | Done (approved) |
| B6 | Answers tool shows keys only, not values; key names match; whole-word "visa" | Done (approved) |
| B7 | Daily limits enforced in the backend (`config/automation.yaml`: total + per platform) | Done |
| B8 | `requirements.txt` (+ `requirements-dev.txt`, exact tested versions) | Done |
| B10 | Profile check; `current_ctc` + `total_experience` answers; search config matches the profile | Done (current CTC saved 2026-10-04) |

## To-do list (remaining work)

"Needs you" marks steps that can't finish without something only you can do.

### Dashboard phase (pages for existing data, REQUIREMENTS 19.4)

Six statuses live in `backend/services/statuses.py` (one mapping for every page). D1 note:
per-platform modes show dry-run until review/auto exist (Phase C/E); logins say "Available
after Phase C".

- [x] D1 Home: mode per platform + live warning banner, today vs daily limits, status counts,
      login status per platform, last run + 10 recent events, quick links
- [x] D2 Jobs: search, sorting, all filters; detail page with dimensions, duplicates, actions
- [x] D3 Applications: filters; detail timeline from the audit log, locked resume badge, masked
      answers, screenshots, errors
- [x] D4 Resumes: master + tailored versions, which job/application uses each, preview,
      download, locked badge
- [x] D5 Logs: search, filters (level, run, platform, date), secrets redacted, download

### Phase C: Naukri

- [x] C1 Common platform interface (login check, search_jobs, get_job_details, apply); one
      selectors file per platform
- [x] C2 "Log in once" command (built: `python -m backend.scripts.login naukri --test-search`) with a persistent browser profile; expired session pauses the
      platform. Needs you: log in to Naukri once (OTP)
- [x] C3 Naukri search from config (live-checked 2026-10-04: 20 cards/page, Naukri's own experience filter) (keywords, locations, experience, freshness, exclusions);
      skip out-of-range experience before scoring; cross-site duplicate check
- [x] C4 Naukri apply, REVIEW MODE ONLY (decision 2026-10-04); "Apply on company site" jobs go to the manual list (company-site flow for them: later). Was: Naukri direct apply (dry-run first); redirects go to the company-site flow or the
      manual-apply list; step cap on multi-step forms
- [x] C5 Needs-my-input: save unanswered questions, answer once, reuse next time (page done)
- [x] C6 Review mode: wait for yes/no before submit (Runs page review mode + Review queue)
- [x] C7 Stop on daily limit or a platform quota/warning message (patterns in each selectors file; exact wording VERIFY when it first appears)
- [x] C8 Reliability: restart the browser after a freeze; clean up sessions of a dead run
- [x] C9 Pages: Review queue, Needs my input, Manual apply, Failed (retry), Runs (start/stop,
      live progress, history), Settings (validated, backed up, diff shown)
- [ ] C10 End-of-run summary. Needs you: a dry-run, then a review-mode run on Naukri

### Phase D: LinkedIn Easy Apply

- [x] D-1 LinkedIn module: login, Easy Apply search (live-checked 2026-10-04: 25 cards/page) and Easy Apply (dry run + review mode; built against a local LinkedIn-shaped page; first live dry run still to do, with the user's OK) (search + Easy Apply), limit 5/day.
      Needs you: log in to LinkedIn once; a dry-run, then a review-mode run

### Phase E: answers, models, auto mode

- [x] E1 Model per task in config (config/automation.yaml models: scoring claude-sonnet-5-5, tailoring/answers claude-opus-5-5; both checked working in Claude Code 2026-10-04). Measured: one scoring call ~12 s, ~$0.035 API-equivalent; 50 jobs ~$1.75 equivalent, ~10 min. Was: Model per task in config (done right before the first analysis run, with a usage
      estimate and a 50-job cap). Needs you: OK the estimate
- [ ] E2 Claude answers other questions from resume/profile facts only; else needs-my-input
- [ ] E3 Auto mode per platform, only after a successful dry-run and review run; confirmation.
      Needs you: switch it on

### Phase F: later items

- [ ] F1 Gmail: detect replies, interview invites, rejections; update status. Needs you:
      connect Gmail
- [ ] F2 Emails page; Analytics page (per-day chart, funnels, score distribution, reply rates)
- [ ] F3 More platforms (Indeed India, Instahyre, Foundit, Hirist, iimjobs)

## Decisions

- B2: started fresh on SQLite. The old PostgreSQL dump stays in `data/backups/` as an archive.
- `DAILY_RUN_ENABLED=false` stays in `.env` until the first planned analysis run.
- Before the first analysis run: add the model-per-task setting (scoring on claude-sonnet-5-5,
  tailoring and answers on claude-opus-5-5), cap the run at 50 jobs, and give a usage estimate
  first.
- Daily limits in `config/automation.yaml`: total 20, naukri 15, linkedin 5, company_site 10.
  Counted: live submission attempts today (any outcome except "user says not sent"); dry runs
  never count. Checked before a live session opens and again right before the submit click.
  `daily_application_limit` in the profile is no longer used.
- B10: standard answers are work_authorization, notice_period, current_ctc, expected_salary,
  willing_to_relocate, total_experience (from `total_experience_years`, only if set). A job
  can't become ready to apply until all are present. Skill-specific experience questions
  ("years with Python") are never answered with total experience.
- B6 includes the agent answers tool fix plus a test proving no answer values appear in the
  agent's action log.
- Phase C: restart the browser after a freeze.
- Experience-range filter moved to Phase C.
- `min_match_score` is 70 in `config/shortlist_rules.yaml` (it was null). The dashboard's
  "Move to shortlist" can override other shortlist rules, never the minimum score.
- 2026-10-03: work through the to-do list without a plan check-in per phase; stop only for
  steps that need the user, before deleting files or big rewrites. Dry-run stays the default.

## Blockers and open questions

- 2026-10-03: Naukri's bot protection (Akamai) answers an automated headless browser with
  "403 Access Denied", even for a public search page. PIE will not try to get around it
  (no stealth plugins, fake fingerprints or user-agent tricks). Next check, with the user:
  the "log in once" command opens a normal, visible browser with PIE's own profile; the user
  logs in by hand; then PIE tries one search page in that same visible browser. If Naukri
  still blocks it, Naukri stays a manual-apply platform (PIE finds and scores jobs from other
  sources; the user applies on Naukri).
- 2026-10-04: the user gave the go-ahead to automate LinkedIn Easy Apply, knowing LinkedIn's
  User Agreement does not allow it and the account may be restricted. Safeguards: limit 5/day
  (lowest), visible browser, the user's own manual login, stop at any security check, no
  disguising the browser. Next: the user runs
  `python -m backend.scripts.login linkedin --test-search` (and the same for naukri); then PIE
  reads the real, logged-in pages to verify selectors and build search + apply.
- 2026-10-04: both logins work and neither site blocked PIE's visible browser. Search works on
  both. Next: Naukri direct apply (C4) and LinkedIn Easy Apply, dry run first.
- 2026-10-04 decisions: Naukri "Apply" usually sends the profile at once, so Naukri applying is
  REVIEW MODE ONLY: the job waits in the Review queue and only the user's "yes" clicks Apply;
  follow-up questions are answered from verified answers, else paused. No Naukri dry-run apply.
  LinkedIn: the user allowed opening ONE Easy Apply form to read its layout and discarding it.

## Environment notes

- Windows Application Control blocks some compiled Python files on this laptop (psycopg, and
  since 2026-10-02 SQLAlchemy's). SQLAlchemy 2.1.1 in `.venv` is installed as a pure-Python
  build: `DISABLE_SQLALCHEMY_CEXT=1 pip install --force-reinstall --no-deps --no-binary
  sqlalchemy "sqlalchemy==2.1.1"`. Redo this after any reinstall of SQLAlchemy.
- Full Python suite: about 7 minutes (820 tests). Dashboard: `npm run check` in `dashboard/`.
