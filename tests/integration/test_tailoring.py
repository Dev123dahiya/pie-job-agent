"""Resume tailoring end to end: tool -> Claude (scripted) -> validation -> version -> readiness.

Every test uses the read-only `master_file` fixture, whose teardown asserts it is unchanged.
"""

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.tools import ToolContext, build_default_registry
from agent.tools.base import ToolErrorCode
from backend.api import deps
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.agent_runner import AgentLimits, AgentRunner, start_agent_run
from database.enums import ToolPermission
from database.models import AgentAction, AgentRun, Application, ApplicationEvent, Job, ResumeVersion
from tests.conftest import FakeClaude, decision, good_resume

RW = frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE})


def make_ctx(
    db: Session,
    settings: Settings,
    profile: UserProfile,
    claude: FakeClaude,
    allowed: frozenset[ToolPermission] = RW,
) -> ToolContext:
    return ToolContext(
        db=db, settings=settings, profile=profile, allowed=allowed, claude_factory=lambda: claude
    )


def tailor(ctx: ToolContext, job_id: Any, **args: Any):  # type: ignore[no-untyped-def]
    return build_default_registry().execute(
        "create_tailored_resume", {"job_id": str(job_id), **args}, ctx
    )


def tailored_rows(db: Session) -> list[ResumeVersion]:
    return list(
        db.scalars(
            select(ResumeVersion)
            .where(ResumeVersion.is_master.is_(False))
            .order_by(ResumeVersion.version_number)
        )
    )


@pytest.fixture
def job_id(shortlisted_job_id: Any) -> Any:
    return shortlisted_job_id  # mock-001: SHORTLISTED with an analysis


def test_creates_validated_version_with_artifacts_and_reports(
    db: Session,
    tailor_settings: Settings,
    tailor_profile: UserProfile,
    job_id: Any,
    master_file: Path,
) -> None:
    claude = FakeClaude(good_resume())
    result = tailor(make_ctx(db, tailor_settings, tailor_profile, claude), job_id)
    assert result.ok, result.message
    data = result.data or {}
    assert (
        data["created"] is True
        and data["validation_status"] == "VALIDATED"
        and data["attempts"] == 1
    )

    master = db.scalar(select(ResumeVersion).where(ResumeVersion.is_master.is_(True)))
    version = db.get(ResumeVersion, uuid.UUID(data["resume_version_id"]))
    assert master is not None and version is not None
    assert master.file_path == str(master_file) and master.validation_status.value == "MASTER"
    assert (
        version.source_version_id == master.id
        and version.version_number == master.version_number + 1
    )
    assert (
        version.job_id == job_id and version.company and version.role == "Backend Software Engineer"
    )
    assert version.source_sha256 == hashlib.sha256(master_file.read_bytes()).hexdigest()
    assert version.tailoring_strategy == "balanced" and version.prompt_version == "tailor-v2"

    pdf, md = Path(version.file_path), Path(version.file_path).with_suffix(".md")
    assert pdf.read_bytes().startswith(b"%PDF") and md.read_text(encoding="utf-8").startswith(
        "# Test Candidate"
    )
    assert version.content_sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert pdf.parent == tailor_settings.resumes_dir / "tailored" / f"job-{job_id}"
    assert f"_v{version.version_number}.pdf" in pdf.name

    changes = version.change_details or {}
    assert changes["facts_preserved"] is True and changes["skills_added"] == []
    assert changes["summary_changed"] is True and "Leave Portal" in changes["projects_removed"]
    assert changes["unsupported_claims_rejected"] == []
    report = version.tailoring_report or {}
    assert "Python" in report["matching_verified_skills"]
    assert not [k for k in report if "score" in k.lower() or k.lower() == "ats"]  # no ATS score
    assert "no ats outcome is guaranteed" in report["note"].lower()
    assert "Go" not in md.read_text(encoding="utf-8").split()  # never on the resume


def test_claude_gets_sources_and_never_go_as_verified(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    claude = FakeClaude(good_resume())
    tailor(make_ctx(db, tailor_settings, tailor_profile, claude), job_id)
    prompt = claude.prompts[0]
    for section in ("MASTER_RESUME_TEXT", "VERIFIED_PROFILE", "JOB:", "ANALYSIS", "STRATEGY"):
        assert section in prompt
    verified = prompt.split("VERIFIED_PROFILE:\n")[1].split("\n\nUNCONFIRMED_SKILLS_NEVER_MENTION")[
        0
    ]
    assert '"Go"' not in verified
    assert '"Go"' in prompt.split("UNCONFIRMED_SKILLS_NEVER_MENTION:\n")[1].split("\n\n")[0]


@pytest.mark.parametrize(
    "bad",
    [
        good_resume(skills=[{"category": "Languages", "items": ["Python", "Go"]}]),
        good_resume(summary="Backend developer skilled in Golang."),
        good_resume(skills=[{"category": "Cloud", "items": ["Kubernetes"]}]),
        good_resume(summary="Improved throughput by 45%."),
    ],
)
def test_failed_validation_saves_nothing(
    db: Session,
    tailor_settings: Settings,
    tailor_profile: UserProfile,
    job_id: Any,
    bad: dict[str, Any],
) -> None:
    result = tailor(make_ctx(db, tailor_settings, tailor_profile, FakeClaude(bad, bad)), job_id)
    assert result.error_code is ToolErrorCode.VALIDATION_FAILED
    violations = (result.data or {})["details"]["violations"]
    assert violations and {v["attempt"] for v in violations} == {"1", "2"}
    assert tailored_rows(db) == []
    assert not list((tailor_settings.resumes_dir / "tailored").rglob("*.*"))
    event = db.scalar(
        select(ApplicationEvent).where(ApplicationEvent.event_type == "resume_tailoring_rejected")
    )
    assert event is not None and event.evidence and event.evidence["violations"]


def test_go_rejection_is_explicit(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    bad = good_resume(skills=[{"category": "Languages", "items": ["Go"]}])
    result = tailor(make_ctx(db, tailor_settings, tailor_profile, FakeClaude(bad, bad)), job_id)
    assert {v["code"] for v in (result.data or {})["details"]["violations"]} == {
        "UNCONFIRMED_SKILL"
    }


def test_one_regeneration_with_feedback(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    bad = good_resume(summary="Deployed services on Kubernetes.")
    claude = FakeClaude(bad, good_resume())
    result = tailor(make_ctx(db, tailor_settings, tailor_profile, claude), job_id)
    assert result.ok and (result.data or {})["attempts"] == 2
    assert "REJECTED" in claude.prompts[1] and "Kubernetes" in claude.prompts[1]
    version = tailored_rows(db)[0]
    rejected = (version.change_details or {})["unsupported_claims_rejected"]
    assert rejected and rejected[0]["code"] == "UNSUPPORTED_TECHNOLOGY"
    assert (version.validation_report or {})["attempts"] == 2


def test_malformed_output_then_valid(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    claude = FakeClaude("{not json", good_resume())
    assert tailor(make_ctx(db, tailor_settings, tailor_profile, claude), job_id).ok


def test_duplicate_protection_and_new_versions_never_overwrite(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    claude = FakeClaude(good_resume(), good_resume())
    ctx = make_ctx(db, tailor_settings, tailor_profile, claude)
    first = tailor(ctx, job_id).data or {}
    again = tailor(ctx, job_id).data or {}
    assert again["created"] is False and again["resume_version_id"] == first["resume_version_id"]
    assert len(claude.prompts) == 1  # no second Claude call for identical inputs
    first_bytes = Path(first["artifact_path"]).read_bytes()

    forced = tailor(ctx, job_id, force_new=True).data or {}
    assert forced["created"] is True and forced["version_number"] == first["version_number"] + 1
    assert Path(forced["artifact_path"]) != Path(first["artifact_path"])
    assert Path(first["artifact_path"]).read_bytes() == first_bytes  # older version untouched
    assert len(tailored_rows(db)) == 2


def test_existing_artifact_is_never_overwritten(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    target = (
        tailor_settings.resumes_dir
        / "tailored"
        / f"job-{job_id}"
        / f"resume_{job_id.hex[:12]}_v2.md"
    )
    target.parent.mkdir(parents=True)
    target.write_text("someone else's file", encoding="utf-8")
    result = tailor(
        make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume())), job_id
    )
    assert result.error_code is ToolErrorCode.PRECONDITION_FAILED and "already exists" in (
        result.message or ""
    )
    assert target.read_text(encoding="utf-8") == "someone else's file"
    assert tailored_rows(db) == []


def test_markdown_only_format(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    data = (
        tailor(
            make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume())),
            job_id,
            requested_format="markdown",
            tailoring_strategy="projects_first",
        ).data
        or {}
    )
    assert data["artifact_path"].endswith(".md") and not list(
        Path(data["artifact_path"]).parent.glob("*.pdf")
    )


def test_draft_reaches_ready_to_apply_only_with_validated_resume(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    ctx = make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume()))
    registry = build_default_registry()
    draft = registry.execute("create_application_draft", {"job_id": str(job_id)}, ctx).data or {}
    assert draft["job_status"] == "SHORTLISTED" and draft["blockers"] == []
    assert any("tailored resume" in s for s in draft["pending_steps"])
    not_ready = registry.execute(
        "update_application_status",
        {"job_id": str(job_id), "target_status": "READY_TO_APPLY", "reason": "try"},
        ctx,
    )
    assert not_ready.error_code is ToolErrorCode.PRECONDITION_FAILED

    result = tailor(ctx, job_id)
    assert result.ok and (result.data or {})["readiness"]["ready"] is True
    assert ("SHORTLISTED", "READY_TO_APPLY") in [
        (c["previous"], c["new"]) for c in result.state_changes
    ]
    db.expire_all()
    job = db.get(Job, job_id)
    app = db.scalar(select(Application))
    assert job is not None and job.pipeline_state.value == "READY_TO_APPLY"
    assert app is not None and app.resume_version_id == tailored_rows(db)[0].id
    assert app.applied_at is None and app.status.value == "DRAFT"  # nothing submitted


def test_resume_first_then_draft_links_it(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    ctx = make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume()))
    version = (tailor(ctx, job_id).data or {})["version_number"]
    draft = (
        build_default_registry()
        .execute("create_application_draft", {"job_id": str(job_id)}, ctx)
        .data
        or {}
    )
    assert draft["tailored_resume_version"] == version and draft["job_status"] == "READY_TO_APPLY"


def test_missing_user_information_blocks(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    tailor_profile.verified_profile.verified_answers.pop("work_authorization")
    ctx = make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume()))
    build_default_registry().execute("create_application_draft", {"job_id": str(job_id)}, ctx)
    result = tailor(ctx, job_id)
    readiness = (result.data or {})["readiness"]
    assert readiness["ready"] is False and any(
        "work_authorization" in b for b in readiness["user_blockers"]
    )
    db.expire_all()
    assert db.get(Job, job_id).pipeline_state.value == "BLOCKED_NEEDS_USER"  # type: ignore[union-attr]


def test_rejected_job_can_be_tailored_but_is_not_eligible(
    db: Session,
    tailor_settings: Settings,
    tailor_profile: UserProfile,
    seeded: None,
    profile: UserProfile,
    sources_config: Any,
) -> None:
    from agent.analysis.analyzers import HeuristicAnalyzer
    from backend.services.analysis import AnalysisContext, analyze_single
    from backend.services.shortlist import ShortlistRules

    job = db.scalar(select(Job).where(Job.source_job_id == "mock-002"))  # Senior Staff ML
    assert job is not None
    analyze_single(
        db,
        job,
        AnalysisContext(analyzer=HeuristicAnalyzer(), profile=profile, rules=ShortlistRules()),
        dev_mode=True,
    )
    assert job.pipeline_state.value == "REJECTED_BY_AGENT"
    result = tailor(
        make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume())), job.id
    )
    assert result.ok and (result.data or {})["readiness"]["eligible"] is False
    db.expire_all()
    assert db.get(Job, job.id).pipeline_state.value == "REJECTED_BY_AGENT"  # type: ignore[union-attr]
    assert db.scalar(select(func.count()).select_from(Application)) == 0


def test_requires_an_analysis_and_existing_job(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, seeded: None
) -> None:
    job = db.scalar(select(Job).where(Job.source_job_id == "mock-003"))  # DISCOVERED, not analyzed
    ctx = make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume()))
    assert tailor(ctx, job.id).error_code is ToolErrorCode.PRECONDITION_FAILED  # type: ignore[union-attr]
    missing = tailor(ctx, "00000000-0000-0000-0000-000000000009")
    assert missing.error_code is ToolErrorCode.NOT_FOUND
    assert (
        tailor(ctx, job.id, tailoring_strategy="make_it_up").error_code
        is ToolErrorCode.INVALID_INPUT
    )  # type: ignore[union-attr]


def test_write_permission_required(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    claude = FakeClaude(good_resume())
    ro = make_ctx(
        db, tailor_settings, tailor_profile, claude, frozenset({ToolPermission.READ_ONLY})
    )
    assert tailor(ro, job_id).error_code is ToolErrorCode.PERMISSION_DENIED
    assert claude.prompts == [] and tailored_rows(db) == []
    tool = build_default_registry().get("create_tailored_resume")
    assert tool is not None and tool.permission is ToolPermission.WRITE  # internal, not external


def test_version_retrieval_tools(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    ctx = make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume()))
    vid = (tailor(ctx, job_id).data or {})["resume_version_id"]
    registry = build_default_registry()
    detail = registry.execute("get_resume_version", {"resume_version_id": vid}, ctx).data or {}
    assert detail["version"]["validation_status"] == "VALIDATED" and detail["content"]["summary"]
    listed = registry.execute("list_resume_versions", {"job_id": str(job_id)}, ctx).data or {}
    assert [v["id"] for v in listed["versions"]] == [vid]


def test_agent_prepares_application_and_stops_at_ready_to_apply(
    db: Session,
    session_factory: sessionmaker[Session],
    tailor_settings: Settings,
    tailor_profile: UserProfile,
    job_id: Any,
) -> None:
    # One FakeClaude serves both the agent's decisions and the tailoring call, in order.
    claude = FakeClaude(
        decision(tool="create_tailored_resume", summary="Tailor resume", job_id=str(job_id)),
        good_resume(),
        decision(tool="create_application_draft", summary="Create draft", job_id=str(job_id)),
        decision(action="complete", message="Ready to apply; nothing submitted."),
    )
    run = start_agent_run(db, "Prepare my application", max_iterations=6)
    AgentRunner(
        session_factory,
        claude,
        settings=tailor_settings,
        profile_loader=lambda: tailor_profile,
        limits=AgentLimits(backoff_seconds=0),
        sleep=lambda s: None,
    ).execute(run.id)
    db.expire_all()
    assert db.get(AgentRun, run.id).status.value == "COMPLETED"  # type: ignore[union-attr]
    assert db.get(Job, job_id).pipeline_state.value == "READY_TO_APPLY"  # type: ignore[union-attr]
    tools = [
        a.tool
        for a in db.scalars(
            select(AgentAction).where(
                AgentAction.run_id == run.id, AgentAction.tool_name == "TOOL_CALL_COMPLETED"
            )
        )
    ]
    assert tools == ["create_tailored_resume", "create_application_draft"]
    assert (
        db.scalar(select(func.count()).select_from(Job).where(Job.pipeline_state == "SUBMITTED"))
        == 0
    )


# --- API ---------------------------------------------------------------------------------


@pytest.fixture
def api(
    client: TestClient, tailor_settings: Settings, tailor_profile: UserProfile
) -> dict[str, Any]:
    from backend.core.config import get_settings

    holder: dict[str, Any] = {"claude": FakeClaude(good_resume())}
    app = client.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_settings] = lambda: tailor_settings
    app.dependency_overrides[deps.profile_dep] = lambda: tailor_profile
    app.dependency_overrides[deps.agent_claude_dep] = lambda: holder["claude"]
    return holder


def test_api_tailor_list_detail_and_download(
    client: TestClient, api: dict[str, Any], job_id: Any
) -> None:
    res = client.post(
        f"/api/jobs/{job_id}/tailored-resume", json={"tailoring_strategy": "skills_first"}
    )
    assert res.status_code == 200, res.text
    vid = res.json()["data"]["resume_version_id"]
    versions = client.get("/api/resumes/versions", params={"job_id": str(job_id)}).json()
    assert [v["id"] for v in versions] == [vid] and versions[0][
        "tailoring_strategy"
    ] == "skills_first"
    detail = client.get(f"/api/resumes/versions/{vid}").json()
    assert detail["validation_status"] == "VALIDATED" and set(detail["artifacts"]) == {
        "pdf",
        "markdown",
    }
    assert (
        detail["markdown"].startswith("# Test Candidate")
        and detail["change_details"]["facts_preserved"]
    )
    pdf = client.get(f"/api/resumes/versions/{vid}/artifact", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["tailored_version"] == detail["version_number"]
    assert job["resume"]["master_available"] is True and job["resume"]["latest"]["id"] == vid
    assert client.get("/api/resumes/master").json()["available"] is True


def test_api_validation_failure_and_errors(
    client: TestClient, api: dict[str, Any], job_id: Any
) -> None:
    bad = good_resume(skills=[{"category": "x", "items": ["Go"]}])
    api["claude"] = FakeClaude(bad, bad)
    res = client.post(f"/api/jobs/{job_id}/tailored-resume")
    assert res.status_code == 422 and res.json()["violations"][0]["code"] == "UNCONFIRMED_SKILL"
    assert client.get("/api/resumes/versions").json() == [
        v for v in client.get("/api/resumes/versions").json() if v["is_master"]
    ]
    assert client.post(f"/api/jobs/{uuid.uuid4()}/tailored-resume").status_code == 404
    assert client.get(f"/api/resumes/versions/{uuid.uuid4()}").status_code == 404


# --- experience gap ---------------------------------------------------------------


def test_report_records_experience_gap_without_hiding_it(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    job = db.get(Job, job_id)
    assert job is not None
    job.experience_required = "2-5 years"
    db.flush()
    result = tailor(
        make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume())), job_id
    )
    assert result.ok, result.message
    report = (result.data or {})["tailoring_report"]
    gap = report["experience_gap"]
    assert gap["gap"] is True and gap["required"] == "2-5 years" and gap["candidate"] == "0.4 years"
    assert gap["internships_counted_as_full_time"] is False
    assert report["missing_requirements"][0].startswith("Experience: job requires 2-5 years")
    # Tailoring never changes eligibility: the job stays shortlisted and is judged by the rules.
    db.expire_all()
    assert db.get(Job, job_id).pipeline_state.value == "SHORTLISTED"  # type: ignore[union-attr]


def test_report_experience_gap_met_and_unknown(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    job = db.get(Job, job_id)
    assert job is not None
    job.experience_required = "0-2 years"
    db.flush()
    ctx = make_ctx(db, tailor_settings, tailor_profile, FakeClaude(good_resume(), good_resume()))
    report = (tailor(ctx, job_id).data or {})["tailoring_report"]
    assert report["experience_gap"]["status"] == "meets"
    assert not any(m.startswith("Experience:") for m in report["missing_requirements"])

    job.experience_required, job.requirements = None, ["Strong technical judgment"]
    db.flush()
    report = (tailor(ctx, job_id, force_new=True).data or {})["tailoring_report"]
    assert report["experience_gap"]["gap"] == "unknown"


def test_fabricated_experience_triggers_regeneration(
    db: Session, tailor_settings: Settings, tailor_profile: UserProfile, job_id: Any
) -> None:
    bad = good_resume(summary="Software developer with 3 years of experience in Python.")
    claude = FakeClaude(bad, good_resume())
    result = tailor(make_ctx(db, tailor_settings, tailor_profile, claude), job_id)
    assert result.ok and (result.data or {})["attempts"] == 2
    rejected = (tailored_rows(db)[0].change_details or {})["unsupported_claims_rejected"]
    assert "UNSUPPORTED_EXPERIENCE" in {r["code"] for r in rejected}
    assert "3 years" not in Path(tailored_rows(db)[0].file_path).with_suffix(".md").read_text(
        encoding="utf-8"
    )


def test_api_job_detail_exposes_experience_gap(
    client: TestClient, api: dict[str, Any], job_id: Any
) -> None:
    detail = client.get(f"/api/jobs/{job_id}").json()
    gap = detail["experience_gap"]
    assert gap["required"] == "0-2 years" and gap["status"] == "meets"
    assert gap["candidate"] == "0.4 years" and gap["internships_counted_as_full_time"] is False
