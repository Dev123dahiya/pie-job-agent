"""Tailoring prompt. Bump TAILORING_PROMPT_VERSION whenever the wording changes."""

import json
from typing import Any

from agent.tailoring.schema import STRATEGY_GUIDANCE
from agent.tools.profile import verified_profile_view
from backend.core.profile import UserProfile

TAILORING_PROMPT_VERSION = "tailor-v2"

TAILORING_SYSTEM_PROMPT = """\
You tailor a candidate's resume to one job. The MASTER_RESUME_TEXT and VERIFIED_PROFILE are
the only sources of truth about the candidate. You improve relevance; you never add facts.

You MAY: reorder sections, skills, projects and experience; choose the most relevant items;
drop irrelevant ones; reword bullets and the summary while keeping their factual meaning;
emphasise keywords the candidate genuinely has.

You MUST NOT:
- add any skill, technology, tool, employer, job title, date, degree, certification,
  project, achievement, responsibility or number that is not in the sources;
- change job titles or dates (copy them exactly as in VERIFIED_PROFILE work_experience);
- present project work as professional/production experience;
- use anything from JOB or ANALYSIS as evidence about the candidate - job requirements are
  NOT the candidate's skills;
- mention any UNCONFIRMED skill (for example Go) anywhere, even as a keyword;
- invent or change metrics. Every number you write must appear in the sources.
- state or imply more years of experience than VERIFIED_PROFILE.total_experience_years
  (verified full-time experience), present internships as full-time roles, or try to hide
  or soften a gap against the job's experience requirement.

Output rules:
- skills: group verified skills into short categories, most relevant first.
- experience: only roles from VERIFIED_PROFILE.work_experience, with company, title,
  start_date, end_date ("" if current) and location copied exactly.
- projects: only projects from VERIFIED_PROFILE.projects; technologies only from that
  project's own list or its description.
- education: copy from VERIFIED_PROFILE.education.
- certifications: only VERIFIED_PROFILE certifications (may be empty).
- publications: only publications named in MASTER_RESUME_TEXT.
- keywords_emphasized: job keywords you emphasised that the candidate verifiably has.
- change_notes: up to 8 short factual notes on what you changed (no reasoning).
Return only the JSON object.
"""


def _text(body: Any) -> str:
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1)


def build_tailoring_message(
    *,
    profile: UserProfile,
    master_text: str,
    job: dict[str, Any],
    analysis: dict[str, Any] | None,
    strategy: str,
    feedback: str | None = None,
) -> str:
    facts = profile.verified_profile
    sections: dict[str, Any] = {
        "STRATEGY": {"name": strategy, "guidance": STRATEGY_GUIDANCE[strategy]},
        "VERIFIED_PROFILE": {
            **verified_profile_view(profile),
            "work_experience": [w.model_dump(mode="json") for w in facts.work_experience],
        },
        "UNCONFIRMED_SKILLS_NEVER_MENTION": facts.unconfirmed_skills,
        "MASTER_RESUME_TEXT": master_text,
        "JOB": job,
        "ANALYSIS": analysis or "(no analysis available)",
    }
    text = "\n\n".join(f"{name}:\n{_text(body)}" for name, body in sections.items())
    if feedback:
        text += (
            "\n\nYOUR PREVIOUS RESUME WAS REJECTED by validation for these unsupported claims:\n"
            f"{feedback}\nRemove or correct every one of them and return the full resume again."
        )
    return text
