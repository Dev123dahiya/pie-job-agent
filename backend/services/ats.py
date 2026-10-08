"""Local ATS check of a tailored resume against its job. Offline, free, private: nothing is
uploaded anywhere (decision 2026-10-04: no online ATS checkers).

Real ATS products (Workday, Greenhouse, Lever...) publish no score, so this checks what they
are known to rely on and reports it plainly:
- the PDF's text can be extracted (what an ATS parser reads), in a sensible length
- standard section headings (Experience / Education / Skills / Projects)
- contact details are present as text (checked, never returned)
- no images, single column (PIE's generator writes one text column, no tables)
- keyword coverage: the job's skills found in the resume. Missing ones are split into skills
  the user HAS (in the verified profile: worth surfacing) and skills the user does not have
  (never to be added: PIE does not invent experience).
"""

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.profile import UserProfile
from database.models import Job, JobAnalysis, ResumeVersion

HEADINGS = {
    "Experience": r"\b(work\s+)?experience\b|\bemployment\b|\binternships?\b",
    "Education": r"\beducation\b",
    "Skills": r"\b(technical\s+)?skills\b",
    "Projects": r"\bprojects\b",
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class AtsReport:
    version_id: str
    job_id: str | None
    keyword_coverage: int | None  # percent of the job's skills found in the resume
    found: list[str] = field(default_factory=list)
    missing_you_have: list[str] = field(default_factory=list)  # in your profile, not here
    missing_not_in_profile: list[str] = field(default_factory=list)  # never added
    checks: list[Check] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w+#.]){re.escape(term.lower())}(?![\w+#])", text) is not None


def pdf_text(path: Path) -> tuple[str, int]:
    """(extracted text, number of images) of a PDF."""
    reader = PdfReader(str(path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    images = 0
    for page in reader.pages:
        try:
            images += len(page.images)
        except Exception:  # an unreadable image still counts as one
            images += 1
    return text, images


def job_terms(db: Session, job: Job) -> list[str]:
    """The skills the job asks for: extracted from the posting, plus Claude's matching and
    missing skills from the latest analysis. De-duplicated, case-insensitive."""
    analysis = db.scalars(
        select(JobAnalysis)
        .where(JobAnalysis.job_id == job.id)
        .order_by(JobAnalysis.created_at.desc())
    ).first()
    terms = [*(job.skills or [])]
    if analysis is not None:
        terms += [*(analysis.matching_skills or []), *(analysis.missing_skills or [])]
    seen: dict[str, str] = {}
    for t in terms:
        t = " ".join(str(t).split())
        if 1 < len(t) <= 60 and t.lower() not in seen:
            seen[t.lower()] = t
    return list(seen.values())


def check_resume(db: Session, version: ResumeVersion, pdf: Path, profile: UserProfile) -> AtsReport:
    text, images = pdf_text(pdf)
    low = text.lower()
    checks = [
        Check(
            "text_readable",
            len(text.strip()) >= 400,
            f"{len(text.strip())} characters of text can be read",
        ),
        Check(
            "no_images",
            images == 0,
            "no images" if images == 0 else f"{images} image(s): ATS can't read them",
        ),
        Check(
            "single_column",
            True,
            "PIE writes one text column without tables (the layout ATS read best)",
        ),
        Check(
            "contact_details",
            bool(EMAIL.search(text)) and bool(PHONE.search(text)),
            "email and phone are written as text"
            if EMAIL.search(text) and PHONE.search(text)
            else "email or phone not found as text",
        ),
    ]
    for heading, pattern in HEADINGS.items():
        present = re.search(pattern, low) is not None
        state = "found" if present else "not found"
        checks.append(Check(f"heading_{heading.lower()}", present, f"'{heading}' section {state}"))

    job = db.get(Job, version.job_id) if version.job_id else None
    terms = job_terms(db, job) if job else []
    have = {s.lower() for s in profile.verified_profile.all_skills()}
    found = [t for t in terms if _contains(low, t)]
    missing = [t for t in terms if t not in found]
    return AtsReport(
        version_id=str(version.id),
        job_id=str(job.id) if job else None,
        keyword_coverage=round(100 * len(found) / len(terms)) if terms else None,
        found=found,
        missing_you_have=[t for t in missing if t.lower() in have],
        missing_not_in_profile=[t for t in missing if t.lower() not in have],
        checks=checks,
    )
