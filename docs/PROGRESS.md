# Progress

Where the work stands, so any new session can catch up. `docs/REQUIREMENTS.md` is the source of
truth; `docs/PHASE_PLAN.md` lists agreed items pinned to later steps. Update this file at the end
of every step. No personal data here.

## Status

| Phase | Status |
|---|---|
| A: git setup + privacy cleanup | Done (merged to `main`) |
| B: bug fixes + SQLite default + requirements.txt | In progress (branch `phase-b-fixes`) |
| Dashboard (pages for existing data) | Not started |
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
| B8 | `requirements.txt` | To do |
| B10 | Check `config/user_profile.yaml` is complete; match `config/job_sources.yaml` | Needs you: fill the profile |

## To-do list (remaining work)

"Needs you" marks steps that can't finish without something only you can do.

### Dashboard phase (pages for existing data, REQUIREMENTS 19.4)

- [ ] D1 Home: mode per platform + live warning banner, today vs daily limits, status counts,
      login status per platform, last run + 10 recent events, quick links
- [ ] D2 Jobs: search, sorting, all filters; detail page with dimensions, duplicates, actions
- [ ] D3 Applications: filters; detail timeline from the audit log, locked resume badge, masked
      answers, screenshots, errors
- [ ] D4 Resumes: master + tailored versions, which job/application uses each, preview,
      download, locked badge
- [ ] D5 Logs: search, filters (level, run, platform, date), secrets redacted, download

### Phase C: Naukri

- [ ] C1 Common platform interface (login check, search_jobs, get_job_details, apply); one
      selectors file per platform
- [ ] C2 "Log in once" command with a persistent browser profile; expired session pauses the
      platform. Needs you: log in to Naukri once (OTP)
- [ ] C3 Naukri search from config (keywords, locations, experience, freshness, exclusions);
      skip out-of-range experience before scoring; cross-site duplicate check
- [ ] C4 Naukri direct apply (dry-run first); redirects go to the company-site flow or the
      manual-apply list; step cap on multi-step forms
- [ ] C5 Needs-my-input: save unanswered questions, answer once, reuse next time
- [ ] C6 Review mode: wait for yes/no before submit
- [ ] C7 Stop on daily limit or a platform quota/warning message
- [ ] C8 Reliability: restart the browser after a freeze; clean up sessions of a dead run
- [ ] C9 Pages: Review queue, Needs my input, Manual apply, Failed (retry), Runs (start/stop,
      live progress, history), Settings (validated, backed up, diff shown)
- [ ] C10 End-of-run summary. Needs you: a dry-run, then a review-mode run on Naukri

### Phase D: LinkedIn Easy Apply

- [ ] D-1 LinkedIn module behind the same interface (search + Easy Apply), limit 5/day.
      Needs you: log in to LinkedIn once; a dry-run, then a review-mode run

### Phase E: answers, models, auto mode

- [ ] E1 Model per task in config (done right before the first analysis run, with a usage
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
- B6 includes the agent answers tool fix plus a test proving no answer values appear in the
  agent's action log.
- Phase C: restart the browser after a freeze.
- Experience-range filter moved to Phase C.
- 2026-10-03: work through the to-do list without a plan check-in per phase; stop only for
  steps that need the user, before deleting files or big rewrites. Dry-run stays the default.

## Environment notes

- Windows Application Control blocks some compiled Python files on this laptop (psycopg, and
  since 2026-10-02 SQLAlchemy's). SQLAlchemy 2.1.1 in `.venv` is installed as a pure-Python
  build: `DISABLE_SQLALCHEMY_CEXT=1 pip install --force-reinstall --no-deps --no-binary
  sqlalchemy "sqlalchemy==2.1.1"`. Redo this after any reinstall of SQLAlchemy.
- Full Python suite: about 7 minutes (820 tests). Dashboard: `npm run check` in `dashboard/`.
