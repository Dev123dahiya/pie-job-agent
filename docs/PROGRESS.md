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
- [x] E3 Auto mode per platform (built 2026-10-04: Runs page "Auto"; unlocks per platform after one completed dry run + one review application the user approved; typed "AUTO on <platform>: up to N today"; same submit checks), only after a successful dry-run and review run; confirmation.
      Needs you: switch it on

### Phase F: later items

- [ ] F1 Gmail: detect replies, interview invites, rejections; update status. Needs you:
      connect Gmail
- [ ] F2 Emails page; Analytics page (per-day chart, funnels, score distribution, reply rates)
- [ ] F3 More platforms (Indeed India, Instahyre, Foundit, Hirist, iimjobs)

## Decisions

- B2: started fresh on SQLite. The old PostgreSQL dump stays in `data/backups/` as an archive.
- 2026-10-04 `DAILY_RUN_ENABLED=true` and `DAILY_AUTO_APPLY=true` (user asked): at 07:00 IST, while `python -m backend.serve` runs, discover + analyze, then AUTO-apply on each unlocked platform (Naukri, LinkedIn) within daily limits (backend/services/daily_auto.py).
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
- 2026-10-04 first real run (discover + analyze, cap 50): 1,149 postings found, 684 new; only 8
  were fresh enough to score (all rejected: mostly senior LinkedIn roles); ~$0.28 API-equivalent.
  Naukri results were weeks old (relevance order) and LinkedIn showed senior roles: fixed with
  Naukri's jobAge filter and LinkedIn's experience-level filter (f_E). Note: with the 48 h window
  a Naukri "1 day ago" job counts as 48 h old (oldest it can be) and goes stale within minutes;
  only "today" jobs are scored unless the user widens the freshness window.
- 2026-10-04 user choices: freshness window 72 h (profile); min_match_score 80 (was 70);
  searches per run: Naukri 8, LinkedIn 5, spread across all roles first. Second run: 12 new
  jobs, 14 scored, 0 shortlisted (best 68; most fresh jobs are mid/senior roles).
- 2026-10-04: local ATS check of tailored resumes (offline; the user agreed: no online ATS
  sites). LinkedIn Easy Apply uploads the job's tailored resume in review mode only (a dry
  run never uploads); Naukri's one-click Apply always sends the resume saved on Naukri.
- 2026-10-04 user: final match score = average of Claude's score and a fixed-weight dimension
  score (role 30, experience 25, skills 25, location 15, salary 5; strong 1, medium 0.6,
  unknown 0.5, weak 0); min_match_score 65. Re-checking saved analyses: 3 shortlisted.
  User asked for fully automatic applying (no "OK" per application): auto mode, gated per
  platform by REQUIREMENTS 10 (one successful dry run + one successful review application).
- 2026-10-04 first LIVE LinkedIn dry run: the form is NOT in #interop-outlet (that holds
  messaging); the reader now starts from the "Apply to ..." heading. Second try: email and
  phone filled, then paused on a required "Phone country code" dropdown (no saved answer).
- 2026-10-04 user answers saved (reusable): phone country code "India (+91)", "years of
  Engineering experience" "1". Live LinkedIn dry run on "AI Engineer" COMPLETE: every step
  filled up to "Submit application", then discarded; nothing sent. Auto on LinkedIn still
  needs one review-mode application the user approves (live switches in .env).
- 2026-10-04 FIRST REAL APPLICATION: LinkedIn Easy Apply to xhawk (AI Engineer), filled in
  review mode, approved by the user in chat, LinkedIn confirmed it was sent. Auto mode is now
  unlocked on LinkedIn (Naukri / company sites still locked). Live switches are ON in .env.
- 2026-10-04 FIRST AUTO-MODE APPLICATION: LinkedIn, EXL (AI/ML Engineer), submitted with no
  per-application yes after the user answered EXL's screening questions. Sensitive answers
  are now stored per exact question; Needs my input shows dropdown choices.
- 2026-10-04 Naukri: dry run + one approved application (MNC Group). PIE first reported it
  unclear (a "chatbot" element on the page, button read too early); Naukri's page showed
  "Applied", recorded as sent. Auto mode now unlocked on Naukri too. Fix: wait up to 20 s
  for the "Applied" button and trust it. TODO: answer Naukri's question chat (needs one
  live look at a real chat first).
- 2026-10-04 Browser: user wants Google Chrome for every session. `BROWSER_CHANNEL=chrome` in
  .env; the login script now follows that setting (it tried Edge first before). A named
  browser is the only one tried. Edge-made profiles backed up to data/backups/browser-edge-*;
  Naukri + LinkedIn logins still valid in Chrome.
- 2026-10-05 Instahyre (step 1 of 3, search): user logged in (Google sign-in via the new
  plain-Chrome login window). Live findings: search = the opportunities page's own GET
  /api/v1/job_search (skills, jobLocations one per request, years); no date sort; job pages
  carry schema.org datePosted + "N-M Years". User chose freshness 14 days for Instahyre and
  search experience 1 year. InstahyreSource built + tested (live read-only smoke test OK),
  config entry present but `enabled: false` until step 2 (Instahyre as its own platform with
  a daily limit) and step 3 (apply: dry run, one approved application, then Auto).
- 2026-10-05 Instahyre step 2 done: its own platform (limit instahyre: 5 inside total 40,
  apply type instahyre_direct, Home card, filters, Settings field); search enabled in the
  daily run.
- 2026-10-05 Instahyre step 3 (apply) built: "Apply now" is one click (page code:
  update_interest -> "Application sent!"), so naukri_apply is now the shared one-click module
  (ONE_CLICK: naukri, instahyre). Review queue, auto mode, daily auto-apply include Instahyre.
  2026-10-05 LIVE: discover found 17 new (1 fresh in 14 days), Meesho "SDE I - AI" shortlisted
  (68); dry run OK; user approved; first two clicks never happened (1: page still loading,
  2: Playwright refused because the button's wrapper <a> "intercepts pointer events"), both
  checked on the page ("Apply now" still there) and recorded as not sent; fix: trial click
  first, press the button itself only when the page shows it on top, otherwise stop without
  clicking (NOT_CLICKED, attempt dropped); retries after a confirmed "not sent" get a new
  submission key on every platform. Third try: "Application sent!" - CONFIRMED.
  Auto mode is now unlocked on Instahyre (and it is in the daily auto-apply).
- 2026-10-05 foundit: user logged in. Live look (5 searches, ~60 jobs): almost all listings are
  copies of LinkedIn jobs or company career pages, none applied on foundit itself. User chose
  SEARCH-ONLY: FounditSource (page's own /middleware/jobsearch, limit 50; LinkedIn/Naukri/
  Instahyre copies and min experience > 2 dropped; description via /middleware/jobdetail from
  the search page). foundit job pages answer 403 "Access Denied" to automation, so PIE never
  opens them. Shortlisted foundit jobs go straight to Manual apply (MANUAL_ONLY_SOURCES) with
  the employer's apply link. Enabled in the daily run.
- 2026-10-05 Cutshort: login helper added (cutshort.io, logged-out /profile redirects), then
  PAUSED by the user ("leave cutshort for now"). Not searched, not applied. Remaining in the
  agreed order: Indeed, Wellfound; company career pages also paused.
- 2026-10-05 Indeed step 1 (search): user logged in (Google). Search pages work in PIE's
  Chrome and embed their results (window.mosaic.providerData jobcards: jobkey, pubDate, age,
  indeedApplyEnabled...): IndeedSource reads that data (sort=date, fromage=days); job pages
  give full description + exact datePosted but sometimes show a "Just a moment..." bot check
  (403), which only skips that description (never marks Indeed blocked, never evaded).
  Until Indeed's "Easily apply" form is built (step 2), shortlisted Indeed jobs go to Manual
  apply (MANUAL_ONLY_SOURCES). Enabled in the daily run.
- 2026-10-05 Indeed DROPPED (user's choice): while starting the "Easily apply" form, Indeed's
  Cloudflare check ("Just a moment...") appeared on the search page itself for PIE's browser.
  PIE does not get around bot checks, so Indeed was removed (source, selectors, login helper
  entry, read_window_data op, config, tests). No Indeed jobs had been stored; nothing applied.
  The local browser profile data/browser/indeed is left for the user to delete.
- 2026-10-05 Focus on Bangalore + high package (user): Bengaluru is the first search location
  (NCR kept after it; Naukri searches 12, LinkedIn 8 so NCR is still covered) and the first
  preferred location in the profile (backup in data/backups/config). New shortlist rule
  min_salary_lpa: 10 - only a SHOWN salary below 10 LPA rejects; jobs without a shown salary
  are kept. Naukri "x-y Lacs P.A." / "LPA" salaries are now stored as annual INR (never
  guessed). Apply order: shown salaries first, highest first.
- 2026-10-05 Bangalore run: 1316 found / 260 new / 31 analyzed / 10 shortlisted (5 LinkedIn,
  1 Naukri -> company site -> Manual apply, 4 foundit Infosys -> Manual apply). LinkedIn had
  logged PIE out between search and apply: all 5 failed with "No Easy Apply button" (page =
  "Join LinkedIn"). Fix: li_open detects the signed-out page (BrowserError LOGIN); the job
  stays READY_TO_APPLY, LinkedIn's login status becomes expired, the run stops on LinkedIn.
  The 5 jobs were requeued; waiting for the user to log in to LinkedIn again.
- 2026-10-05 After re-login: NxTreasury (AI Engineer) SUBMITTED in auto mode; Intellicredence
  paused ("years of experience in total?"); TantranZm + Emagesoft got stuck on a hidden-radio
  Yes/No question; Hello Parent has no Easy Apply any more. Fixes: the form reader treats a
  hidden radio as visible when its drawn stand-in (role=radio / label) is, takes the option
  text from it and the question from the text before the group; filling clicks the stand-in.
  A LinkedIn job paused for answers goes back to READY_TO_APPLY once they are all saved
  (RESTART_DOMAINS), instead of staying paused.
- 2026-10-05 Retry: Jobgether's Submit click was SENT (LinkedIn now says "Application
  submitted"; PIE only knew "application sent") - checked on the job page, recorded as sent;
  wording added. TantranZm/Emagesoft stuck again: the degree question is drawn after the step
  loads; a step that does not move is now read once more before failing, so the question is
  asked. Today: LinkedIn 2/5 (NxTreasury, Jobgether), Instahyre 1/5 (Meesho).
- 2026-10-05 (evening) User answered: total experience "1", Bachelor's degree "Yes" (saved,
  reused). Live fixes: LinkedIn marks the drawn option with aria-checked and redraws the group
  (PIE's markers vanish) - the choice is confirmed by finding the selected option again by its
  question + text; the Easy Apply button is waited for (drawn late). TantranZm and Emagesoft
  SUBMITTED. Intellicredence shows "Application submitted" on LinkedIn although PIE never
  clicked Submit (no attempt recorded): the paused LIVE form had been left open in the window;
  recorded as applied outside PIE. Paused LinkedIn forms are now always discarded.
  Day total: LinkedIn 4/5 + Intellicredence (outside PIE), Instahyre 1/5.
- Tests no longer read the real .env (one failure printed the Adzuna key into a local test
  log; the user was advised to regenerate it).

## Environment notes

- Windows Application Control blocks some compiled Python files on this laptop (psycopg, and
  since 2026-10-02 SQLAlchemy's). SQLAlchemy 2.1.1 in `.venv` is installed as a pure-Python
  build: `DISABLE_SQLALCHEMY_CEXT=1 pip install --force-reinstall --no-deps --no-binary
  sqlalchemy "sqlalchemy==2.1.1"`. Redo this after any reinstall of SQLAlchemy.
- Full Python suite: about 7 minutes (820 tests). Dashboard: `npm run check` in `dashboard/`.
