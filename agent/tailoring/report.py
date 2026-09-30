"""Deterministic change summary and tailoring report. Computed by PIE by comparing the
tailored resume with the verified baseline - not taken on trust from the model."""

import re
from typing import Any

from agent.tailoring.experience import experience_gap
from agent.tailoring.schema import TailoredResume
from agent.tailoring.validation import Evidence, key, norm

_SECTION_HEADERS = ("technical skills", "experience", "projects", "education", "publications")


def master_summary(master_text: str) -> str:
    """The 'Summary' section of the master resume, if it has one."""
    lower = master_text.lower()
    start = lower.find("summary")
    if start < 0:
        return ""
    ends = [i for h in _SECTION_HEADERS if (i := lower.find(h, start + 7)) > 0]
    return master_text[start + 7 : min(ends) if ends else None].strip()


def change_summary(
    resume: TailoredResume, evidence: Evidence, rejected: list[dict[str, str]]
) -> dict[str, Any]:
    facts = evidence.profile.verified_profile
    base_skills = facts.confirmed_skills()
    tailored_skills = resume.all_skill_items()
    base_keys, tailored_keys = [key(s) for s in base_skills], [key(s) for s in tailored_skills]
    kept_in_base_order = [k for k in base_keys if k in tailored_keys]
    kept_in_new_order = [k for k in tailored_keys if k in base_keys]

    base_projects = [key(p.name) for p in facts.projects]
    new_projects = [key(p.name) for p in resume.projects]
    base_roles = [key(w.company) for w in facts.work_experience]
    new_roles = [key(e.company) for e in resume.experience]

    base_bullets = {norm(h) for w in facts.work_experience for h in w.highlights}
    new_bullets = [b for e in resume.experience for b in e.bullets]
    bullets_changed = sum(1 for b in new_bullets if norm(b) not in base_bullets)

    summary_changed = norm(resume.summary) != norm(master_summary(evidence.master_text))
    sections: list[str] = []
    if summary_changed:
        sections.append("summary")
    if tailored_keys != base_keys:
        sections.append("skills")
    if new_roles != base_roles or bullets_changed:
        sections.append("experience")
    if new_projects != [k for k in base_projects if k in new_projects] or len(new_projects) != len(
        base_projects
    ):
        sections.append("projects")

    return {
        "sections_changed": sections,
        "section_order": list(resume.section_order),
        "summary_changed": summary_changed,
        "skills_reordered": kept_in_base_order != kept_in_new_order,
        "skills_added": [s for s in tailored_skills if key(s) not in base_keys],
        "skills_removed": [s for s in base_skills if key(s) not in tailored_keys],
        "projects_reordered": [k for k in new_projects if k in base_projects]
        != [k for k in base_projects if k in new_projects],
        "projects_removed": [p.name for p in facts.projects if key(p.name) not in new_projects],
        "experience_reordered": new_roles != [k for k in base_roles if k in new_roles],
        "bullets_changed": bullets_changed,
        "keywords_emphasized": list(resume.keywords_emphasized),
        "unsupported_claims_rejected": rejected,
        "facts_preserved": True,  # only set on versions that passed validation
        "notes": [n[:200] for n in resume.change_notes[:8]],
    }


def tailoring_report(
    resume: TailoredResume,
    evidence: Evidence,
    job: dict[str, Any],
    analysis: dict[str, Any] | None,
    changes: dict[str, Any],
) -> dict[str, Any]:
    facts = evidence.profile.verified_profile
    job_text = norm(
        " ".join(
            [
                job.get("title") or "",
                job.get("description") or "",
                *job.get("requirements", []),
                *job.get("skills", []),
            ]
        )
    )

    def in_job(term: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(norm(term))}(?![a-z0-9])", job_text) is not None

    matching_skills = [s for s in facts.all_skills() if in_job(s)]
    matching_experience = [
        f"{w.title} at {w.company}"
        for w in facts.work_experience
        if any(in_job(s) for s in matching_skills if norm(s) in norm(" ".join(w.highlights)))
    ]
    matching_projects = [
        p.name
        for p in facts.projects
        if any(in_job(t) for t in p.technologies)
        or any(in_job(s) for s in matching_skills if norm(s) in norm(p.description))
    ]
    missing = list((analysis or {}).get("missing_skills") or [])
    for skill in facts.unconfirmed_skills:
        if in_job(skill) and skill not in missing:
            missing.append(skill)
    missing = [
        f"{m} (unconfirmed - not claimed)" if evidence.is_unconfirmed(m) else m for m in missing
    ]

    gap = experience_gap(evidence.profile, job)
    if gap["gap"] is True:
        missing.insert(
            0,
            f"Experience: job requires {gap['required']}; verified full-time experience is "
            f"{gap['candidate']} (gap - not concealed)",
        )

    improvements = []
    if changes["skills_reordered"] or changes["skills_removed"]:
        improvements.append(f"{len(matching_skills)} job-relevant verified skills placed first")
    if changes["projects_reordered"] or changes["projects_removed"]:
        improvements.append("Most relevant projects placed first; less relevant ones trimmed")
    if changes["summary_changed"]:
        improvements.append("Summary rewritten toward this role using verified facts only")
    if changes["bullets_changed"]:
        improvements.append(
            f"{changes['bullets_changed']} experience bullets reworded for relevance"
        )
    return {
        "job_requirements": list(
            (analysis or {}).get("important_requirements") or job.get("requirements") or []
        )[:12],
        "matching_verified_skills": matching_skills,
        "matching_experience": matching_experience,
        "matching_projects": matching_projects,
        "missing_requirements": missing,
        "keywords_emphasized": changes["keywords_emphasized"],
        "sections_changed": changes["sections_changed"],
        "relevance_improvements": improvements,
        "experience_gap": gap,
        "note": "Relevance was improved using verified information only. No ATS outcome is "
        "guaranteed and no score is estimated.",
    }
