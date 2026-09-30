"""Deterministic mock data for DEVELOPMENT_MODE and tests.

Every record is clearly fictional (source="demo-seed"). Nothing here touches real sites.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from database.enums import (
    AgentActionStatus,
    AgentRunStatus,
    ApplicationStatus,
    EmailClassification,
    EventSource,
    Freshness,
    JobPipelineState,
    MatchLevel,
    RemoteType,
    RunType,
)
from database.models import (
    AgentAction,
    AgentRun,
    Application,
    ApplicationEvent,
    Email,
    Job,
    JobAnalysis,
)
from database.repositories import CompanyRepository

# (company, title, location, remote, salary_min, pipeline_state, application_status)
_MOCK_JOBS: list[
    tuple[str, str, str, RemoteType, int | None, JobPipelineState, ApplicationStatus | None]
] = [
    (
        "Acme Analytics",
        "Software Engineer I",
        "Gurugram",
        RemoteType.HYBRID,
        1_200_000,
        JobPipelineState.SUBMITTED,
        ApplicationStatus.INTERVIEW,
    ),
    (
        "Nimbus AI",
        "AI Engineer",
        "Remote",
        RemoteType.REMOTE,
        1_500_000,
        JobPipelineState.SUBMITTED,
        ApplicationStatus.ASSESSMENT,
    ),
    (
        "Orbit Data",
        "Data Engineer",
        "Noida",
        RemoteType.ONSITE,
        None,
        JobPipelineState.SUBMITTED,
        ApplicationStatus.APPLICATION_CONFIRMED,
    ),
    (
        "Kite Labs",
        "Backend Engineer",
        "Delhi NCR",
        RemoteType.HYBRID,
        1_000_000,
        JobPipelineState.SUBMITTED,
        ApplicationStatus.REJECTED,
    ),
    (
        "Pixel Forge",
        "SDE 1",
        "Gurugram",
        RemoteType.ONSITE,
        None,
        JobPipelineState.BLOCKED_NEEDS_USER,
        ApplicationStatus.REQUIRES_USER_ACTION,
    ),
    (
        "Quantum Loop",
        "ML Engineer",
        "Remote",
        RemoteType.REMOTE,
        1_800_000,
        JobPipelineState.READY_TO_APPLY,
        ApplicationStatus.SHORTLISTED,
    ),
    (
        "Delta Retail",
        "Data Scientist",
        "Noida",
        RemoteType.HYBRID,
        None,
        JobPipelineState.SHORTLISTED,
        ApplicationStatus.SHORTLISTED,
    ),
    (
        "Legacy Corp",
        "Senior Staff Engineer",
        "Bengaluru",
        RemoteType.ONSITE,
        4_000_000,
        JobPipelineState.REJECTED_BY_AGENT,
        None,
    ),
    (
        "Cloudline",
        "Software Developer",
        "Delhi NCR",
        RemoteType.ONSITE,
        None,
        JobPipelineState.ANALYZED,
        None,
    ),
    (
        "Bright Apps",
        "Frontend Intern",
        "Mumbai",
        RemoteType.ONSITE,
        300_000,
        JobPipelineState.DISCOVERED,
        None,
    ),
]

_REASONS = {
    ApplicationStatus.SHORTLISTED: "Shortlisted by agent analysis",
    ApplicationStatus.APPLIED: "Application submitted (mock form, confirmed)",
    ApplicationStatus.APPLICATION_CONFIRMED: "Confirmation email received",
    ApplicationStatus.ASSESSMENT: "Assessment invitation received",
    ApplicationStatus.INTERVIEW: "Interview invitation received",
    ApplicationStatus.REJECTED: "Rejection email received",
    ApplicationStatus.REQUIRES_USER_ACTION: "Blocked: OTP verification required",
}

_STATUS_PATH = [
    ApplicationStatus.SHORTLISTED,
    ApplicationStatus.APPLIED,
    ApplicationStatus.APPLICATION_CONFIRMED,
    ApplicationStatus.ASSESSMENT,
    ApplicationStatus.INTERVIEW,
]


def seed_mock_data(db: Session, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    companies = CompanyRepository(db)

    run = AgentRun(
        task="Mock discovery + application run",
        run_type=RunType.MOCK,
        status=AgentRunStatus.COMPLETED,
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=1, minutes=40),
        development_mode=True,
        jobs_processed=len(_MOCK_JOBS),
        applications_submitted=4,
        errors=[{"job": "Pixel Forge / SDE 1", "error": "OTP verification required"}],
        summary="Mock run - all actions were logged, none executed against real sites.",
    )
    db.add(run)
    db.flush()

    seq = 0
    for i, (company_name, title, location, remote, salary, state, app_status) in enumerate(
        _MOCK_JOBS
    ):
        discovered = now - timedelta(hours=3 + i)
        job = Job(
            source="demo-seed",
            source_job_id=f"demo-{i + 1:03d}",
            company=companies.get_or_create(company_name),
            title=title,
            location=location,
            remote_type=remote,
            salary_min=salary,
            salary_currency="INR" if salary else None,
            description=f"Mock posting for {title} at {company_name}.",
            url=f"https://example.invalid/jobs/{i + 1}",
            posted_at=discovered - timedelta(hours=6),
            discovered_at=discovered,
            pipeline_state=state,
        )
        db.add(job)
        db.flush()
        db.add(
            ApplicationEvent(
                job_id=job.id,
                occurred_at=discovered,
                event_type="job_discovered",
                source=EventSource.AGENT,
                new_status=JobPipelineState.DISCOVERED.value,
                reason="Demo seed data",
            )
        )

        if state == JobPipelineState.DISCOVERED:
            continue

        should_apply = state != JobPipelineState.REJECTED_BY_AGENT
        db.add(
            JobAnalysis(
                job_id=job.id,
                agent_run_id=run.id,
                match_score=80 - i * 4,
                role_match=MatchLevel.STRONG if should_apply else MatchLevel.WEAK,
                skills_match=MatchLevel.STRONG,
                experience_match=MatchLevel.MEDIUM if should_apply else MatchLevel.WEAK,
                location_match=MatchLevel.STRONG if location != "Bengaluru" else MatchLevel.WEAK,
                salary_match=MatchLevel.UNKNOWN if salary is None else MatchLevel.STRONG,
                freshness=Freshness.FRESH,
                should_shortlist=should_apply,
                shortlisted=should_apply,
                analyzer="mock",
                reason="Mock analysis"
                if should_apply
                else "Requires 8+ years; outside preferred locations",
            )
        )
        seq += 1
        db.add(
            AgentAction(
                run_id=run.id,
                sequence=seq,
                tool_name="analyze_job",
                job_id=job.id,
                input={"job_id": str(job.id)},
                output={"should_apply": should_apply},
                status=AgentActionStatus.DRY_RUN,
                started_at=discovered + timedelta(minutes=5),
            )
        )

        if app_status is None:
            continue

        application = Application(job_id=job.id, status=app_status)
        if state == JobPipelineState.BLOCKED_NEEDS_USER:
            application.requires_user_input = True
            application.user_action_required = (
                "Enter the OTP sent to your phone to continue the application."
            )
        db.add(application)
        db.flush()

        path = (
            _STATUS_PATH[: _STATUS_PATH.index(app_status) + 1]
            if app_status in _STATUS_PATH
            else (
                [*_STATUS_PATH[:3], app_status]
                if app_status == ApplicationStatus.REJECTED
                else [app_status]
            )
        )
        prev: ApplicationStatus | None = None
        for step, status in enumerate(path):
            db.add(
                ApplicationEvent(
                    job_id=job.id,
                    application_id=application.id,
                    occurred_at=discovered + timedelta(minutes=30 + step * 10),
                    event_type="status_change",
                    source=EventSource.EMAIL if step > 1 else EventSource.AGENT,
                    previous_status=prev.value if prev else None,
                    new_status=status.value,
                    reason=f"{job.company.name if job.company else ''}: {_REASONS[status]}",
                )
            )
            prev = status
        if ApplicationStatus.APPLIED in path:
            application.applied_at = discovered + timedelta(minutes=40)

    db.add(
        Email(
            provider_message_id="mock-msg-001",
            sender="talent@nimbus-ai.example",
            subject="Nimbus AI - Online assessment for AI Engineer",
            received_at=now - timedelta(minutes=50),
            classification=EmailClassification.ASSESSMENT,
            company_name="Nimbus AI",
            role="AI Engineer",
            match_confidence=0.95,
            interpretation="Assessment invitation, due in 3 days",
            action_required="Complete the online assessment",
        )
    )
    db.add(
        Email(
            provider_message_id="mock-msg-002",
            sender="recruiter@acme-analytics.example",
            subject="Quick chat about your application?",
            received_at=now - timedelta(minutes=20),
            classification=EmailClassification.RECRUITER,
            company_name="Acme Analytics",
            match_confidence=0.6,
            needs_review=True,
            interpretation="Recruiter outreach; role not stated",
        )
    )
    db.commit()
