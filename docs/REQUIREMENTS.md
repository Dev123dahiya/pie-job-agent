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
