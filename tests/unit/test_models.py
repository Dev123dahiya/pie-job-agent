from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.enums import (
    AgentActionStatus,
    ApplicationStatus,
    EmailClassification,
    EventSource,
    Freshness,
    JobPipelineState,
    MatchLevel,
)
from database.models import (
    AgentAction,
    AgentRun,
    Application,
    ApplicationEvent,
    Email,
    Job,
    JobAnalysis,
    Resume,
    ResumeVersion,
    User,
)
from database.repositories import CompanyRepository, JobRepository


def _job(db: Session, source_job_id: str = "123", source: str = "mock") -> Job:
    job = Job(
        source=source,
        source_job_id=source_job_id,
        company=CompanyRepository(db).get_or_create("Acme  Analytics"),
        title="Software Engineer",
        url=f"https://example.invalid/{source_job_id}",
    )
    db.add(job)
    db.flush()
    return job


def test_job_defaults_and_enum_storage(db: Session) -> None:
    job = _job(db)
    db.commit()
    assert job.pipeline_state is JobPipelineState.DISCOVERED
    assert job.requirements == [] and job.skills == []
    assert job.salary_min is None  # unknown salary is allowed
    raw = db.execute(text("SELECT pipeline_state FROM jobs")).scalar_one()
    assert raw == "DISCOVERED"  # stored as plain string, not a native enum


def test_job_dedupe_constraint(db: Session) -> None:
    _job(db, "dup")
    db.commit()
    with pytest.raises(IntegrityError):
        _job(db, "dup")
    db.rollback()
    # Same source_job_id on another source is a different job.
    _job(db, "dup", source="other")
    db.commit()
    assert JobRepository(db).count() == 2
    assert JobRepository(db).get_by_source_id("mock", "dup") is not None


def test_company_get_or_create_normalizes(db: Session) -> None:
    repo = CompanyRepository(db)
    a = repo.get_or_create("Acme Analytics")
    b = repo.get_or_create("  acme   ANALYTICS ")
    assert a.id == b.id
    assert a.normalized_name == "acme analytics"


def test_analysis_stores_individual_dimensions(db: Session) -> None:
    job = _job(db)
    db.add(
        JobAnalysis(
            job_id=job.id,
            role_match=MatchLevel.STRONG,
            skills_match=MatchLevel.STRONG,
            experience_match=MatchLevel.UNKNOWN,
            location_match=MatchLevel.STRONG,
            salary_match=MatchLevel.UNKNOWN,
            freshness=Freshness.FRESH,
            missing_skills=["Kubernetes"],
            should_shortlist=True,
        )
    )
    db.commit()
    db.refresh(job)
    assert job.analyses[0].salary_match is MatchLevel.UNKNOWN
    assert job.analyses[0].missing_skills == ["Kubernetes"]


def test_resume_versioning_keeps_master(db: Session) -> None:
    user = User(name="Test User", email="test@example.com")
    resume = Resume(user=user, name="Master")
    master = ResumeVersion(
        resume=resume, version_number=1, is_master=True, file_path="resumes/master/master.pdf"
    )
    db.add_all([user, resume, master])
    db.flush()
    job = _job(db)
    tailored = ResumeVersion(
        resume=resume,
        version_number=2,
        file_path="resumes/tailored/acme.pdf",
        source_version_id=master.id,
        job_id=job.id,
        company="Acme",
        role="SWE",
        changes_summary="Reordered skills",
    )
    db.add(tailored)
    db.commit()
    db.refresh(resume)
    assert [v.version_number for v in resume.versions] == [1, 2]
    assert tailored.source_version is master
    assert master.file_path == "resumes/master/master.pdf"

    with pytest.raises(IntegrityError):
        db.add(ResumeVersion(resume=resume, version_number=2, file_path="x"))
        db.commit()


def test_application_timeline_and_email_link(db: Session) -> None:
    job = _job(db)
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()
    assert app.status is ApplicationStatus.SHORTLISTED
    assert app.applied_at is None

    email = Email(
        provider_message_id="m1",
        sender="hr@acme.example",
        subject="Thanks",
        received_at=datetime.now(UTC),
        classification=EmailClassification.APPLICATION_CONFIRMATION,
        application_id=app.id,
        match_confidence=0.97,
    )
    db.add(email)
    db.flush()
    db.add_all(
        [
            ApplicationEvent(
                job_id=job.id,
                event_type="job_discovered",
                source=EventSource.AGENT,
                occurred_at=datetime(2026, 9, 28, 9, tzinfo=UTC),
            ),
            ApplicationEvent(
                job_id=job.id,
                application_id=app.id,
                event_type="status_change",
                source=EventSource.EMAIL,
                previous_status="APPLIED",
                new_status="APPLICATION_CONFIRMED",
                reason="Confirmation email",
                evidence={"subject": "Thanks"},
                email_id=email.id,
                occurred_at=datetime(2026, 9, 28, 10, tzinfo=UTC),
            ),
        ]
    )
    db.commit()
    db.refresh(app)
    assert len(app.events) == 1  # the discovery event belongs to the job, pre-application
    assert app.events[0].evidence == {"subject": "Thanks"}
    assert app.emails[0].provider_message_id == "m1"


def test_agent_run_logs_actions_in_order(db: Session) -> None:
    run = AgentRun(task="test")
    run.actions = [
        AgentAction(sequence=2, tool_name="open_page", status=AgentActionStatus.DRY_RUN),
        AgentAction(
            sequence=1,
            tool_name="search_jobs",
            input={"q": "sde"},
            status=AgentActionStatus.SUCCESS,
        ),
    ]
    db.add(run)
    db.commit()
    db.expire_all()
    loaded = db.get(AgentRun, run.id)
    assert loaded is not None
    assert [a.tool_name for a in loaded.actions] == ["search_jobs", "open_page"]
    assert loaded.errors == []


def test_datetimes_round_trip_as_aware_utc(db: Session) -> None:
    from datetime import timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    job = _job(db)
    job.posted_at = datetime(2026, 9, 28, 10, 0, tzinfo=ist)
    db.commit()
    db.expire_all()
    loaded = db.get(Job, job.id)
    assert loaded is not None and loaded.posted_at is not None
    assert loaded.posted_at.tzinfo is not None
    assert loaded.posted_at == datetime(2026, 9, 28, 4, 30, tzinfo=UTC)


def test_naive_datetimes_rejected(db: Session) -> None:
    job = _job(db)
    job.posted_at = datetime(2026, 9, 28, 10, 0)
    with pytest.raises(Exception, match="Naive datetimes"):
        db.commit()
    db.rollback()
