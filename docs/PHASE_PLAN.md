# Phase plan: agreed items not to forget

`docs/REQUIREMENTS.md` is the source of truth. This file only records items that were agreed
during the work and moved to (or pinned to) a later step.

## Phase B (in progress)

- B6: the agent's `get_application_answers` tool returns only which answers exist and which
  are missing, never the values. Test: no answer value appears in the agent's action log.
- Right before the first real analysis run: model per task in config (scoring
  claude-sonnet-5-5; tailoring and application answers claude-opus-5-5). Then estimate the
  usage for the run (capped at 50 jobs) before running it. `DAILY_RUN_ENABLED=false` stays in
  `.env` until then.
- B10: the user fills `config/user_profile.yaml`; check it is complete (name the exact missing
  fields) and update `config/job_sources.yaml` to match.

## Phase C (Naukri)

- Skip jobs clearly outside the experience range before scoring (REQUIREMENTS 5).
- Done (C8): Restart the browser after a freeze: when a browser call times out, the browser thread may
  stay stuck and every later step waits for its own timeout (about 2.5 minutes). Detect this,
  restart the browser, and fail only the job that froze.
- Done (C8): Sessions of a run whose backend process died (e.g. a restart mid-run) can stay active in
  the database; clean them up (e.g. at startup or on the Failed / Runs pages).
