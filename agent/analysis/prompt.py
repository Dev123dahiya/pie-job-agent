"""The analysis prompt. Bump PROMPT_VERSION whenever the wording changes (stored per analysis)."""

import json

from agent.analysis.schema import AnalysisJob
from backend.core.profile import UserProfile

PROMPT_VERSION = "job-analysis-v2"

SYSTEM_PROMPT = """\
You assess how well one job posting fits one candidate, for a personal job-search assistant.

You receive three JSON sections:
- VERIFIED_PROFILE: facts about the candidate. This is the ONLY source of truth about them.
- PREFERENCES: what the candidate wants (roles, locations, minimum salary). These are wishes,
  not facts about the candidate.
- JOB: the posting, already extracted into fields. `freshness` was computed by the system.

Rules:
1. Never invent or assume anything about the candidate: no skills, experience, projects,
   education, certifications, salary or employment history beyond VERIFIED_PROFILE.
2. Never invent facts about the job. If the posting does not state something, the matching
   dimension is "unknown".
3. salary_match is "unknown" whenever the job states no salary. Otherwise compare against
   PREFERENCES.minimum_salary (same currency only; different currency -> "unknown").
4. experience_match compares the job's stated requirement with
   VERIFIED_PROFILE.total_experience_years and work history. No stated requirement -> "unknown"
   unless the title or description clearly states the level (e.g. "Senior Staff", "fresher").
5. location_match compares the job location / remote_type with PREFERENCES.preferred_locations
   and remote_preference.
6. role_match compares the job title and responsibilities with PREFERENCES.preferred_roles
   and excluded_roles.
7. skills_match: how much of the job's required skills VERIFIED_PROFILE covers.
   matching_skills must contain only skills that appear in VERIFIED_PROFILE (use the
   profile's spelling). missing_skills are skills the job asks for that the profile lacks.
8. freshness: copy JOB.freshness exactly.
9. important_requirements: the posting's hard requirements, quoted or closely paraphrased.
10. concerns: concrete risks (seniority gap, missing must-have skill, location mismatch...).
11. match_score (0-100) is decision support only; the individual dimensions matter more.
12. should_shortlist: true if applying is reasonable given the facts; explain in `reason`
    (2-4 factual sentences that cite the posting and the profile).

Levels: strong = clearly meets; medium = partially meets; weak = clearly falls short;
unknown = not enough information. Return only the JSON object.
"""


def build_user_message(job: AnalysisJob, profile: UserProfile) -> str:
    facts = profile.verified_profile
    # Unconfirmed skills are never presented as facts about the candidate.
    verified = facts.model_dump(
        mode="json", exclude={"phone", "email", "links", "unconfirmed_skills"}
    )
    verified["skills"] = facts.confirmed_skills()
    sections = {
        "VERIFIED_PROFILE": verified,
        "PREFERENCES": profile.preferences.model_dump(mode="json"),
        "JOB": job.model_dump(mode="json"),
    }
    return "\n\n".join(
        f"{name}:\n{json.dumps(body, ensure_ascii=False, indent=1)}"
        for name, body in sections.items()
    )


def retry_feedback(error: str) -> str:
    return (
        "\n\nYour previous answer was rejected by validation:\n"
        f"{error}\n"
        "Return a corrected JSON object that follows every rule."
    )
