# Requirements (source of truth)

Below are the full requirements for my job-application tool. First, save this whole message as docs/REQUIREMENTS.md in the project, and treat it as the source of truth for all future work. If anything in the code conflicts with it, tell me.

=== 1. GOAL ===
A personal tool that runs on my own laptop, finds jobs matching my profile on multiple platforms, and applies to them automatically. It should be accurate and honest, never submit false information, and stay within safe daily limits.

=== 2. PROJECT BASE ===
- Extend the existing PIE project. Do not start a new one.
- Switch the default database to SQLite so Docker isn't required. Keep PostgreSQL as an option.
- Keep everything that already works: duplicate detection, Claude match scoring, verified answers, the dry-run guard, audit logs, the dashboard, and the tests.

=== 3. JOB PLATFORMS ===
Build in this order:
1. Naukri: search and direct apply
2. LinkedIn: search and Easy Apply
3. Keep the existing company-career-site applying (Greenhouse, Lever, Ashby, etc.) and the existing API job sources
Later: Indeed India, Instahyre, Foundit, Hirist, iimjobs
Each platform gets its own module behind one common interface (login check, search_jobs, get_job_details, apply), with all of its page selectors in one file so they're easy to update when the site changes.

=== 4. LOGIN ===
- A "log in once" command that opens a browser with a persistent profile. I log in manually (including OTP). The tool reuses that session.
- Never store my Naukri or LinkedIn passwords anywhere.
- If a session expires, pause that platform and tell me to log in again. Don't try to log in automatically.

=== 5. JOB SEARCH ===
- Keywords, locations, experience range, job freshness, and excluded companies/keywords all come from config.
- Skip jobs already processed on any platform, including the same job posted on multiple sites.
- Skip jobs clearly outside my experience range before scoring.

=== 6. MATCHING ===
- Claude scores every job 0-100 with a short reason.
- Only apply if the score is at or above min_match_score (default [70]) AND the shortlist rules pass.
- Use a cheaper model (claude-sonnet-5-5 or claude-haiku-4-5) for scoring, and claude-opus-5-5 for resume tailoring and answering questions. Model names go in config.

=== 7. RESUME ===
- Master resume: resumes/master/[filename].pdf
- Tailored resumes per job, with every claim checked against the master resume (this already exists; keep it).
- Once an application is submitted or in progress, its linked resume must never change.

=== 8. ANSWERING APPLICATION QUESTIONS ===
- Standard answers come from config/user_profile.yaml: total experience, current CTC, expected CTC, notice period, current location, willingness to relocate, work authorization, etc.
- For any other question, Claude answers using ONLY facts from my master resume and profile.
- If the answer isn't clearly supported by my resume or profile, never guess. Pause that job as "needs my input," and save the question so I can answer it once and it gets reused next time.
- Never exaggerate or invent experience, skills, salary, or qualifications.

=== 9. APPLYING ===
- Naukri direct-apply and LinkedIn Easy Apply: fill and submit on the platform.
- Jobs that redirect to a company website: use PIE's company-site flow if that site is supported. Otherwise, add them to a manual-apply list with the link.
- Multi-step forms: cap the number of steps. If the form gets stuck, stop and mark it failed.

=== 10. MODES AND LIMITS ===
- dry-run (default): does everything except the final submit.
- review: shows me each filled application and waits for my yes/no.
- auto: submits without asking, only for platforms I enable it on, and only after I've tested that platform in dry-run and review modes.
- Daily limits, actually enforced: a total limit plus a separate one per platform. LinkedIn's limit should be lower than Naukri's. Stop when a limit is reached, and stop immediately if a platform shows a daily quota or warning message.
- Live mode must never be switched on by accident. Keep the existing double switch.

=== 11. TRACKING AND REPORTS ===
- Every job saved with: platform, title, company, location, URL, match score and reason, status (found / skipped / applied / needs input / manual / failed), date, resume used, and error message.
- End of each run: a summary with counts per platform and status, a list of jobs needing my input, the manual-apply list, and failures.
- Show all of this in the dashboard.

=== 12. RELIABILITY ===
- On any failure: close the browser session, take a screenshot, log the error, mark the job failed, and continue with the next job.
- Failed jobs must not be re-scored and re-shortlisted automatically. I'll retry them myself from the dashboard or with a command.
- Limited retries only.

=== 13. PRIVACY AND SECURITY ===
- Initialise git before any changes. .env, config/user_profile.yaml, resumes/, data/, and the browser profile folder must be in .gitignore.
- No personal data in code or tests. Remove my real name from agent/execution/answers.py and tests/unit/test_field_mapping.py.
- Don't store my answers (salary, notice period, etc.) in browser-session records.
- Keep secrets redacted in logs.

=== 14. KNOWN BUGS TO FIX FIRST ===
- Failed browser runs stay stuck "in progress"
- Failed jobs get re-scored and re-shortlisted
- Resume can be swapped on submitted applications
- Answers stored in session data
- daily_application_limit is not enforced
- Mismatched answer key names between modules
- The "visa" keyword matches words like "advisable"

=== 15. LATER (after the above works) ===
- Gmail integration: detect replies, interview invites, and rejections, and update application status.
- Fill in the Emails, Logs, and Settings dashboard pages.
- Add more platforms from section 3.

=== 16. ORDER OF WORK ===
Phase A: git setup + privacy cleanup (section 13)
Phase B: bug fixes (section 14) + SQLite default + requirements.txt
Phase C: Naukri
Phase D: LinkedIn Easy Apply
Phase E: LLM answers for other questions + model split + auto mode
Phase F: items in section 15

=== 17. HOW TO WORK WITH ME ===
- Before each phase, show me a short plan and wait for my approval.
- Work in small steps. Run the tests after each step, and keep them all passing.
- After each step, tell me what changed and how to test it in dry-run mode.
- Ask me before deleting files or rewriting large parts of the code.
- Explain things in simple language.

=== 18. MY DETAILS ===
- Target roles: [e.g., Python Developer, Backend Developer]
- Locations: [e.g., Gurugram, Delhi NCR, Noida, Remote]
- Total experience: [X years]
- Current CTC: [X LPA]
- Expected CTC: [X LPA]
- Notice period: [X days]
- Willing to relocate: [Yes/No]
- Companies to exclude: [optional]

=== 19. DASHBOARD ===

--- 19.1 General rules ---
- Keep the existing stack: Next.js + React dashboard, FastAPI backend. Don't switch frameworks. Ask me before adding any new library.
- All safety rules live in the backend, never only in the dashboard: daily limits, dry-run guard, min score, live-mode double switch, never-guess answers. The dashboard can never bypass them.
- The dashboard runs only on my laptop: bind to 127.0.0.1, never 0.0.0.0.
- Every page has proper loading, empty and error states. Pages for features that aren't built yet show a clear "Available after Phase X" message instead of fake data.
- Show my six statuses (found / skipped / applied / needs input / manual / failed) everywhere, with PIE's detailed status available underneath.
- Clean, consistent design with light and dark mode. Desktop-first, but usable on a phone screen.
- Pages refresh automatically every few seconds while a run is in progress.

--- 19.2 Pages ---

1. Home (overview)
- Current mode per platform (dry-run / review / auto), shown clearly at the top. If live mode is on anywhere, show a visible warning banner.
- Today's applications vs daily limits: total and per platform (Naukri, LinkedIn, company sites), as progress bars.
- Counts per status for today and for all time.
- Login status per platform (logged in / expired / never logged in), with the exact command to log in again.
- Last run summary and the 10 most recent events.
- Quick links: "Needs my input (X)", "Manual apply (X)", "Failed (X)", "Waiting for review (X)".

2. Jobs
- Table with search, sorting and filters: platform, status, match score range, date, company, location, apply type.
- Job detail page: full description, match score and reason, the individual scoring dimensions, apply type, duplicates found on other sites, and a link to the original posting.
- Actions: skip, move to shortlist, retry analysis, open original.

3. Review queue
- Every application waiting for my yes/no in review mode.
- Shows the job, match score and reason, the resume that will be used, and the answers that will be filled (label + "Filled from: notice_period", with AI-suggested answers clearly marked).
- For Naukri: shows what will be sent before Apply is clicked.
- Buttons: Approve, Reject, Open job. Approve/reject one at a time, or several at once, with a confirmation.

4. Applications
- Table of all applications with filters by platform, status and date.
- Detail page: timeline of every step from the audit log, resume used (locked badge if submitted or in progress), answers used (masked as above), screenshots, and errors.

5. Needs my input
- Every paused job with the exact question(s) it couldn't answer.
- A form to answer each question once. The answer is saved to my reusable saved answers, and the job goes back into the queue.
- Show which saved answers already exist, and let me edit or delete them.

6. Manual apply list
- Jobs I need to apply to myself, with company, title, score, and a direct link.
- "Mark as applied" and "Skip" buttons, so they're tracked like the others.

7. Failed
- Failed jobs with the error, the stage it failed at, the screenshot, and the date.
- A "Retry" button per job (manual only, as in section 12) and "Retry all selected".

8. Resumes
- My master resume, all tailored versions, which job each was made for, and which applications use it.
- Preview and download buttons, and a locked badge on resumes linked to submitted or in-progress applications.

9. Runs (agent control)
- Start a run: choose the steps (discover / analyze / tailor / apply), the platforms, and the mode. Dry-run is always pre-selected.
- Switching to live requires a second confirmation that names the platform and the remaining daily limit.
- A Stop button that safely stops after the current job.
- Live progress while running, and a history of past runs with their summaries.

10. Emails (after Gmail integration, Phase F)
- Replies, interview invites and rejections, each linked to its application.
- The application's status updates automatically, and I can correct it.

11. Logs
- Searchable logs with filters for level, run, platform and date.
- Secrets always redacted.
- Download a filtered log file.

12. Settings
- Edit search settings: keywords, locations, experience range, job freshness, excluded companies and keywords.
- Edit limits: total and per platform.
- Edit min_match_score and the model used for each task.
- Mode per platform. Auto mode can only be switched on for a platform after it has at least one successful dry-run and one successful review-mode run, and needs a confirmation.
- Edit my standard profile answers.
- Every save: validate the values, back up the old config file, show me what changed, then save.

13. Analytics
- Applications per day (chart), by platform.
- Funnel per platform: found → shortlisted → applied → replied → interview.
- Match score distribution, and top skipped reasons.
- Reply rate and interview rate once Gmail is connected.

--- 19.3 Backend and testing ---
- Every page gets FastAPI endpoints with proper input validation. No business logic in the frontend.
- Tests for every new endpoint, including tests proving the dashboard can't bypass limits, dry-run or the live-mode switch.
- The full test suite stays green after every step.

--- 19.4 Order ---
Don't build the dashboard as one big block. Build each page alongside the backend feature it shows:
- Pages that only need existing data (Jobs, Applications, Resumes, Logs, Home basics): add them as a Dashboard phase right after Phase B.
- Review queue, Needs my input, Manual apply, Failed, Runs: build them with Phase C (Naukri), since that's when they get real data.
- Settings: build during Phase C, since the limits and modes come in then.
- Emails and Analytics reply rates: Phase F.
