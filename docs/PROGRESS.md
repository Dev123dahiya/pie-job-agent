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
| B5 | A resume linked to an in-progress or submitted application never changes | Done (committed, waiting for approval) |
| B6 | Answers tool shows keys only, not values; key names match; whole-word "visa" | Done (committed, waiting for approval) |
| B7+ | Daily limit enforcement, requirements.txt, user profile check (B10) | Not started |

## Decisions

- B2: started fresh on SQLite. The old PostgreSQL dump stays in `data/backups/` as an archive.
- `DAILY_RUN_ENABLED=false` stays in `.env` until the first planned analysis run.
- Before the first analysis run: add the model-per-task setting (scoring on claude-sonnet-5-5,
  tailoring and answers on claude-opus-5-5), cap the run at 50 jobs, and give a usage estimate
  first.
- Daily limits for `config/automation.yaml`: total 20, naukri 15, linkedin 5, company_site 10.
  (The file doesn't exist yet. It gets created when the limits are enforced.)
- B6 includes the agent answers tool fix plus a test proving no answer values appear in the
  agent's action log.
- Phase C: restart the browser after a freeze.
- Experience-range filter moved to Phase C.

## Environment notes

- Windows Application Control blocks some compiled Python files on this laptop (psycopg, and
  since 2026-10-02 SQLAlchemy's). SQLAlchemy 2.1.1 in `.venv` is installed as a pure-Python
  build: `DISABLE_SQLALCHEMY_CEXT=1 pip install --force-reinstall --no-deps --no-binary
  sqlalchemy "sqlalchemy==2.1.1"`. Redo this after any reinstall of SQLAlchemy.
- Full Python suite: about 7 minutes (820 tests). Dashboard: `npm run check` in `dashboard/`.
