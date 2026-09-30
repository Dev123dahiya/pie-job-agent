"""Resume tailoring: MASTER RESUME + JOB + ANALYSIS -> Claude -> validation -> new version.

- The master resume is only ever read. Tailored versions are new, immutable rows and new
  files (created with exclusive-create; an existing file is never overwritten).
- Output that fails validation is never stored as a version: the failure is recorded in the
  job timeline, Claude gets one controlled regeneration with the exact violations, and a
  second failure returns a structured error.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.claude_client import ClaudeCallError, StructuredClaude
from agent.tailoring.prompt import (
    TAILORING_PROMPT_VERSION,
    TAILORING_SYSTEM_PROMPT,
    build_tailoring_message,
)
from agent.tailoring.report import change_summary, tailoring_report
from agent.tailoring.schema import TAILORED_RESUME_SCHEMA, TailoringOutputError, parse_tailored
from agent.tailoring.validation import Evidence, Violation, validate_tailored, validation_report
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.analysis import profile_fingerprint
from backend.services.readiness import Readiness, apply_readiness, evaluate_readiness
from backend.services.resume_render import render_markdown, render_pdf, write_new_file
from backend.services.resumes import MasterResume, ResumeUnavailableError, read_master_resume
from database.enums import EventSource, JobPipelineState, ResumeValidationStatus
from database.models import (
    Application,
    ApplicationEvent,
    Job,
    JobAnalysis,
    Resume,
    ResumeVersion,
    User,
)

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 2  # the first attempt plus one controlled regeneration
NOT_TAILORABLE = {JobPipelineState.DUPLICATE, JobPipelineState.WITHDRAWN}


class TailoringError(RuntimeError):
    def __init__(self, code: str, message: str, violations: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.code = code  # NOT_FOUND | PRECONDITION_FAILED | VALIDATION_FAILED | EXECUTION_FAILED
        self.message = message
        self.violations = violations or []


@dataclass
class TailoringResult:
    version: ResumeVersion
    created: bool
    attempts: int
    readiness: Readiness | None
    rejected: list[dict[str, str]] = field(default_factory=list)


def _latest_analysis(db: Session, job: Job) -> JobAnalysis | None:
    return db.scalar(
        select(JobAnalysis)
        .where(JobAnalysis.job_id == job.id)
        .order_by(JobAnalysis.created_at.desc())
        .limit(1)
    )


def _master_version(db: Session, profile: UserProfile, master: MasterResume) -> ResumeVersion:
    """Register (once per file content) the master resume as a MASTER version row."""
    facts = profile.verified_profile
    user = db.scalar(select(User).where(User.email == str(facts.email)))
    if user is None:
        user = User(name=facts.name, email=str(facts.email))
        db.add(user)
        db.flush()
    resume = db.scalar(
        select(Resume).where(Resume.user_id == user.id, Resume.name == "Master resume")
    )
    if resume is None:
        resume = Resume(user_id=user.id, name="Master resume")
        db.add(resume)
        db.flush()
    existing = db.scalar(
        select(ResumeVersion).where(
            ResumeVersion.resume_id == resume.id,
            ResumeVersion.is_master.is_(True),
            ResumeVersion.content_sha256 == master.sha256,
        )
    )
    if existing is not None:
        return existing
    version = ResumeVersion(
        resume_id=resume.id,
        version_number=_next_version(db, resume.id),
        is_master=True,
        file_path=str(master.path),
        content_sha256=master.sha256,
        validation_status=ResumeValidationStatus.MASTER,
        source_sha256=master.sha256,
        changes_summary="Master resume (read-only source of truth).",
    )
    db.add(version)
    db.flush()
    return version


def _next_version(db: Session, resume_id: uuid.UUID) -> int:
    current = db.scalar(
        select(func.max(ResumeVersion.version_number)).where(ResumeVersion.resume_id == resume_id)
    )
    return (current or 0) + 1


def _job_payload(job: Job) -> dict[str, Any]:
    return {
        "title": job.title,
        "company": job.company.name if job.company else None,
        "location": job.location,
        "experience_required": job.experience_required,
        "requirements": job.requirements,
        "skills": job.skills,
        "description": (job.description or "")[:6000],
    }


def _analysis_payload(a: JobAnalysis | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "important_requirements": a.important_requirements,
        "matching_skills": a.matching_skills,
        "missing_skills": a.missing_skills,
        "role_match": a.role_match.value,
        "skills_match": a.skills_match.value,
        "shortlisted": a.shortlisted,
    }


def _event(
    db: Session,
    job: Job,
    app: Application | None,
    event_type: str,
    reason: str,
    evidence: dict[str, Any],
    actor: str,
) -> None:
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id if app else None,
            event_type=event_type,
            source=EventSource.USER if actor == "user" else EventSource.AGENT,
            reason=reason,
            evidence=evidence,
        )
    )


def tailor_resume(
    db: Session,
    *,
    settings: Settings,
    profile: UserProfile,
    claude: StructuredClaude,
    job_id: uuid.UUID,
    application_id: uuid.UUID | None = None,
    strategy: str = "balanced",
    requested_format: str = "pdf",
    force_new: bool = False,
    vocabulary: list[str] | None = None,
    actor: str = "agent",
    state_changes: list[Any] | None = None,
) -> TailoringResult:
    job = db.get(Job, job_id)
    if job is None:
        raise TailoringError("NOT_FOUND", f"No job with id {job_id}")
    if job.pipeline_state in NOT_TAILORABLE:
        raise TailoringError("PRECONDITION_FAILED", f"Job is {job.pipeline_state.value}")
    analysis = _latest_analysis(db, job)
    if analysis is None:
        raise TailoringError("PRECONDITION_FAILED", "Analyze the job first (analyze_job)")
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    if application_id is not None and (app is None or app.id != application_id):
        raise TailoringError("PRECONDITION_FAILED", "application_id does not belong to this job")
    try:
        master = read_master_resume(settings)
    except ResumeUnavailableError as exc:
        raise TailoringError("PRECONDITION_FAILED", str(exc)) from exc

    profile_hash = profile_fingerprint(profile)
    master_version = _master_version(db, profile, master)

    if not force_new:  # duplicate protection: same inputs -> same version
        existing = db.scalar(
            select(ResumeVersion)
            .where(
                ResumeVersion.job_id == job.id,
                ResumeVersion.validation_status == ResumeValidationStatus.VALIDATED,
                ResumeVersion.source_sha256 == master.sha256,
                ResumeVersion.profile_sha256 == profile_hash,
                ResumeVersion.tailoring_strategy == strategy,
                ResumeVersion.artifact_format == requested_format,
                ResumeVersion.prompt_version == TAILORING_PROMPT_VERSION,
            )
            .order_by(ResumeVersion.version_number.desc())
            .limit(1)
        )
        if existing is not None:
            readiness = _link(db, settings, profile, job, app, existing, actor, state_changes)
            db.commit()
            return TailoringResult(existing, created=False, attempts=0, readiness=readiness)

    evidence = Evidence(
        master.text, profile, [*(vocabulary or []), *job.skills, *analysis.missing_skills]
    )
    job_payload, analysis_payload = _job_payload(job), _analysis_payload(analysis)
    rejected: list[dict[str, str]] = []
    feedback: str | None = None
    resume = None
    model = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = build_tailoring_message(
            profile=profile,
            master_text=master.text,
            job=job_payload,
            analysis=analysis_payload,
            strategy=strategy,
            feedback=feedback,
        )
        try:
            reply = claude.call(TAILORING_SYSTEM_PROMPT, message, TAILORED_RESUME_SCHEMA)
        except ClaudeCallError as exc:
            raise TailoringError("EXECUTION_FAILED", f"Claude call failed: {exc}") from exc
        model = reply.model
        try:
            candidate = parse_tailored(reply.payload)
            violations = validate_tailored(candidate, evidence)
        except TailoringOutputError as exc:
            candidate, violations = None, [Violation("MALFORMED_OUTPUT", str(exc))]
        if not violations:
            resume = candidate
            break
        found = [x.as_dict() for x in violations]
        rejected += [{**x, "attempt": str(attempt)} for x in found]
        feedback = "\n".join(f"- {x['code']}: {x['detail']}" for x in found)
        logger.info(
            "Tailored resume for job %s rejected (attempt %s): %s", job.id, attempt, feedback
        )

    if resume is None:
        _event(
            db,
            job,
            app,
            "resume_tailoring_rejected",
            f"Tailored resume failed validation {MAX_ATTEMPTS} times; nothing was saved",
            {"violations": rejected},
            actor,
        )
        db.commit()
        raise TailoringError(
            "VALIDATION_FAILED",
            f"The tailored resume made unsupported claims {MAX_ATTEMPTS} times; "
            "no version was saved.",
            rejected,
        )

    content = resume.model_dump(mode="json")
    changes = change_summary(resume, evidence, rejected)
    report = tailoring_report(resume, evidence, job_payload, analysis_payload, changes)
    number = _next_version(db, master_version.resume_id)
    folder = settings.resumes_dir / "tailored" / f"job-{job.id}"
    stem = f"resume_{job.id.hex[:12]}_v{number}"
    markdown = render_markdown(content, profile).encode("utf-8")
    primary = folder / f"{stem}.{'pdf' if requested_format == 'pdf' else 'md'}"
    if primary.resolve() == master.path.resolve():
        raise TailoringError("PRECONDITION_FAILED", "Refusing to write over the master resume")
    written = []
    try:
        write_new_file(folder / f"{stem}.md", markdown)
        written.append(folder / f"{stem}.md")
        if requested_format == "pdf":
            write_new_file(primary, render_pdf(content, profile))
            written.append(primary)
    except FileExistsError as exc:
        raise TailoringError(
            "PRECONDITION_FAILED", f"Artifact already exists: {exc.filename}"
        ) from exc

    version = ResumeVersion(
        resume_id=master_version.resume_id,
        version_number=number,
        is_master=False,
        file_path=str(primary),
        content_sha256=hashlib.sha256(primary.read_bytes()).hexdigest(),
        source_version_id=master_version.id,
        job_id=job.id,
        company=job.company.name if job.company else None,
        role=job.title,
        changes_summary="; ".join(changes["notes"][:4]) or "Reordered and reworded for this job.",
        validation_status=ResumeValidationStatus.VALIDATED,
        validation_report={
            **validation_report([]),
            "attempts": 2 if rejected else 1,
            "rejected_attempts": rejected,
        },
        tailoring_strategy=strategy,
        artifact_format=requested_format,
        content=content,
        change_details=changes,
        tailoring_report=report,
        source_sha256=master.sha256,
        profile_sha256=profile_hash,
        model=model,
        prompt_version=TAILORING_PROMPT_VERSION,
    )
    try:
        db.add(version)
        db.flush()
        _event(
            db,
            job,
            app,
            "tailored_resume_created",
            f"Tailored resume v{number} created and validated (internal only)",
            {"resume_version_id": str(version.id), "version": number},
            actor,
        )
        readiness = _link(db, settings, profile, job, app, version, actor, state_changes)
        db.commit()
    except Exception:
        db.rollback()
        for path in written:  # keep disk and database consistent
            path.unlink(missing_ok=True)
        raise
    return TailoringResult(
        version,
        created=True,
        attempts=1 if not rejected else 2,
        readiness=readiness,
        rejected=rejected,
    )


def _link(
    db: Session,
    settings: Settings,
    profile: UserProfile,
    job: Job,
    app: Application | None,
    version: ResumeVersion,
    actor: str,
    state_changes: list[Any] | None,
) -> Readiness | None:
    """Attach the version to the job's draft (if any) and re-evaluate readiness."""
    if app is None:
        return evaluate_readiness(db, settings, profile, job, None)
    app.resume_version_id = version.id
    db.flush()
    readiness = evaluate_readiness(db, settings, profile, job, app)
    apply_readiness(db, job, app, readiness, actor=actor, state_changes=state_changes)
    return readiness
