"""Browser execution against the LOCAL fixture site (tests/fixtures/apply_site.py).

Real Playwright, headless, 127.0.0.1 only. Nothing here contacts an employer. The central
guarantees under test:
- EXTERNAL_DRY_RUN can never produce SUBMITTED (and never posts anything to the site);
- EXTERNAL_LIVE cannot run unless explicitly enabled, and needs the user's confirmation;
- CAPTCHA / OTP / 2FA / identity / login / unknown questions stop for the user;
- duplicates and repeated submits are refused; SUBMITTED needs positive confirmation.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.tools import ToolContext, build_default_registry
from agent.tools.base import ToolErrorCode, ToolResult
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services import execution as svc
from database.base import utcnow
from database.enums import (
    BrowserExecutionMode,
    BrowserSessionState,
    EventSource,
    ExecutionMode,
    ExternalActionResult,
    JobPipelineState,
    SubmissionOutcome,
    ToolPermission,
)
from database.models import (
    Application,
    ApplicationEvent,
    BrowserSession,
    ExternalActionLog,
    Job,
    SubmissionAttempt,
)
from integrations.browser import BrowserError, BrowserManager
from tests.conftest import FakeClaude, good_resume
from tests.fixtures.apply_site import ApplySite

S = JobPipelineState
B = BrowserSessionState
ALL = frozenset(ToolPermission)
REG = build_default_registry()
DRY, LIVE = ExecutionMode.EXTERNAL_DRY_RUN, ExecutionMode.EXTERNAL_LIVE


@pytest.fixture(scope="module")
def site() -> Iterator[ApplySite]:
    s = ApplySite().start()
    yield s
    s.stop()


@pytest.fixture
def entered(browser: BrowserManager, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """field_id -> the text or option label PIE put on the page, recorded at fill time."""
    seen: dict[str, str] = {}
    real_fill, real_select = browser.fill, browser.combobox_select

    def fill(sid: str, field_id: str, kind: str, value: str | None, option: int | None) -> bool:
        ok = real_fill(sid, field_id, kind, value, option)
        seen[field_id] = f"option {option}" if option is not None else str(value)
        return ok

    def combobox_select(sid: str, field_id: str, index: int, expected: str) -> str:
        seen[field_id] = expected
        return real_select(sid, field_id, index, expected)

    monkeypatch.setattr(browser, "fill", fill)
    monkeypatch.setattr(browser, "combobox_select", combobox_select)
    return seen


@pytest.fixture(scope="module")
def browser() -> Iterator[BrowserManager]:
    manager = BrowserManager(headless=True, timeout_ms=15_000)
    yield manager
    manager.shutdown()


@pytest.fixture(autouse=True)
def _clear_site(site: ApplySite) -> None:
    site.submissions.clear()


@pytest.fixture
def exec_profile(tailor_profile: UserProfile) -> UserProfile:
    facts = tailor_profile.verified_profile.model_copy(
        update={
            "links": {
                "linkedin": "https://linkedin.example/in/test",
                "portfolio": "https://portfolio.example",
            },
            "verified_answers": {
                **tailor_profile.verified_profile.verified_answers,
                "first_name": "Test",
                "last_name": "Candidate",
                "work_authorization": "Yes - authorized to work in India",
            },
        }
    )
    return tailor_profile.model_copy(update={"verified_profile": facts})


@pytest.fixture
def exec_settings(tailor_settings: Settings, tmp_path: Path) -> Settings:
    return tailor_settings.model_copy(
        update={"application_artifacts_dir": tmp_path / "artifacts", "external_live_enabled": False}
    )


@pytest.fixture
def live_settings(exec_settings: Settings) -> Settings:
    """A deliberate PRODUCTION configuration, used only against the local fixture site:
    DEVELOPMENT_MODE=false + EXTERNAL_LIVE_ENABLED=true + a visible browser. (The injected test
    BrowserManager stays headless; the setting is what PIE enforces.)"""
    return exec_settings.model_copy(
        update={"development_mode": False, "external_live_enabled": True, "browser_headless": False}
    )


@pytest.fixture
def dev_live_settings(live_settings: Settings) -> Settings:
    """DEVELOPMENT_MODE=true with EXTERNAL_LIVE_ENABLED=true: live must STILL be blocked."""
    return live_settings.model_copy(update={"development_mode": True})


def tctx(
    db: Session, settings: Settings, profile: UserProfile, browser: Any, mode: ExecutionMode = DRY
) -> ToolContext:
    return ToolContext(
        db=db,
        settings=settings,
        profile=profile,
        allowed=ALL,
        execution_mode=mode,
        browser=browser,
        claude_factory=lambda: FakeClaude(good_resume()),
    )


def call(ctx: ToolContext, name: str, **args: Any) -> ToolResult:
    return REG.execute(
        name, {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in args.items()}, ctx
    )


@pytest.fixture
def ready_job(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    shortlisted_job_id: Any,
    site: ApplySite,
    browser: BrowserManager,
) -> uuid.UUID:
    """mock-001 -> validated tailored resume + draft -> READY_TO_APPLY, pointing at the site."""
    job = db.get(Job, shortlisted_job_id)
    assert job is not None
    job.location, job.application_url = "Gurugram, India", site.url("normal")
    db.commit()
    ctx = tctx(db, exec_settings, exec_profile, browser, ExecutionMode.INTERNAL_WRITES)
    assert call(ctx, "create_tailored_resume", job_id=job.id).ok
    assert call(ctx, "create_application_draft", job_id=job.id).ok
    db.refresh(job)
    assert job.pipeline_state is S.READY_TO_APPLY
    return job.id


def use_case(db: Session, job_id: uuid.UUID, site: ApplySite, case: str) -> None:
    job = db.get(Job, job_id)
    assert job is not None
    job.application_url = site.url(case)
    db.commit()


def ectx(
    db: Session, settings: Settings, profile: UserProfile, browser: Any, live: bool = False
) -> svc.ExecContext:
    return svc.ExecContext(
        db=db,
        settings=settings,
        profile=profile,
        browser=browser,
        mode=BrowserExecutionMode.LIVE if live else BrowserExecutionMode.DRY_RUN,
        actor="user",
    )


def job_state(db: Session, job_id: uuid.UUID) -> S:
    db.expire_all()
    job = db.get(Job, job_id)
    assert job is not None
    return job.pipeline_state


def prepare_live(
    db: Session, settings: Settings, profile: UserProfile, browser: Any, job_id: uuid.UUID
) -> uuid.UUID:
    out = svc.run_execution(ectx(db, settings, profile, browser, live=True), job_id)
    assert out["state"] == "READY_FOR_SUBMISSION", out
    return uuid.UUID(out["session_id"])


def confirm(db: Session, settings: Settings, sid: uuid.UUID) -> None:
    svc.confirm_live_submission(db, settings, sid, "I confirm this submission")


# --- dry run ------------------------------------------------------------------------------


def test_dry_run_end_to_end_never_submits(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["banner"] == "DRY RUN — Nothing has been submitted."
    assert out["dry_run_complete"] is True and out["state"] == "CLOSED"
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and session.prepared_at is not None
    filled = {f["key"] for f in session.filled_fields}
    assert {
        "first_name",
        "last_name",
        "email",
        "phone",
        "linkedin",
        "portfolio",
        "work_authorization",
        "willing_to_relocate",
        "notice_period",
        "expected_salary",
    } <= filled
    skipped = {f["label"] for f in session.skipped_fields}
    assert {"How did you hear about us?", "Gender", "Cover Letter"} <= skipped
    assert session.uploaded_resume and session.uploaded_resume["file_name"].endswith(".pdf")
    labels = [s["label"] for s in session.screenshots]
    assert labels[:4] == ["opened", "form_detected", "fields_completed", "pre_submission"]
    for shot in session.screenshots:
        assert (exec_settings.application_artifacts_dir / shot["file"]).is_file()
    # Nothing reached the employer; nothing can claim submission.
    assert site.submissions == []
    assert job_state(db, ready_job) is S.READY_TO_APPLY
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert app is not None and app.applied_at is None
    results = set(db.scalars(select(ExternalActionLog.result)).all())
    assert ExternalActionResult.DRY_RUN in results
    events = set(db.scalars(select(ApplicationEvent.event_type)).all())
    assert "dry_run_completed" in events and "application_submitted" not in events


def _answer_values(profile: UserProfile) -> dict[str, str]:
    """The user's own answers that a form could receive (short yes/no values excluded)."""
    facts = profile.verified_profile
    values = {"email": str(facts.email), "phone": facts.phone or ""}
    values |= {f"link {k}": v for k, v in facts.links.items()}
    values |= {k: v for k, v in facts.verified_answers.items() if len(v) >= 6}
    return {k: v for k, v in values.items() if v}


def _without_page_options(data: Any) -> Any:
    """Drop the page's own option lists: they are the employer's choices, not answers."""
    if isinstance(data, dict):
        return {k: _without_page_options(v) for k, v in data.items() if k != "options"}
    if isinstance(data, list):
        return [_without_page_options(v) for v in data]
    return data


def _leaks(profile: UserProfile, records: Any) -> list[str]:
    import json

    text = json.dumps(_without_page_options(records), ensure_ascii=False, default=str)
    return [k for k, v in _answer_values(profile).items() if v in text]


def test_answers_are_never_stored_in_session_records(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    """REQUIREMENTS 13: salary, notice period etc. are not kept in browser-session records.
    The fields are filled on the page, and PIE records only which answer key went in."""
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["dry_run_complete"] is True
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and len(session.filled_fields) >= 10
    for record in [*session.detected_fields, *session.filled_fields]:
        assert not {"value", "option", "selected"} & set(record), record
    stored = {
        "detected_fields": session.detected_fields,
        "filled_fields": session.filled_fields,
        "missing_answers": session.missing_answers,
        "skipped_fields": session.skipped_fields,
        "challenges": session.challenges,
        "current_step": session.current_step,
        "logs": [(x.summary, x.error) for x in db.scalars(select(ExternalActionLog))],
        "events": [(e.reason, e.evidence) for e in db.scalars(select(ApplicationEvent))],
        "result": out,
    }
    assert _leaks(exec_profile, stored) == []


def test_dry_run_step_by_step_and_submit_is_refused(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    ctx = tctx(db, exec_settings, exec_profile, browser)
    opened = call(ctx, "open_application", job_id=ready_job)
    assert opened.ok and opened.data and opened.data["mode"] == "DRY_RUN"
    sid = opened.data["session_id"]
    assert job_state(db, ready_job) is S.APPLICATION_IN_PROGRESS
    again = call(ctx, "open_application", job_id=ready_job)  # idempotent
    assert again.ok and again.data and again.data["session_id"] == sid and again.data["reused"]
    form = call(ctx, "inspect_application_form", session_id=sid)
    assert form.ok and form.data and form.data["submit_available"] is True
    assert form.data["missing_required_fields"] == [] and form.data["challenges"] == []
    fill = call(ctx, "fill_application_field", session_id=sid, use_mapping=True)
    assert fill.ok and fill.data and len(fill.data["filled"]) >= 10
    again = call(ctx, "fill_application_field", session_id=sid, use_mapping=True)
    assert again.ok and again.data and again.data["filled"] == []  # idempotent
    up = call(ctx, "upload_application_resume", session_id=sid)
    assert up.ok and up.data and up.data["uploaded"]["file_name"].endswith(".pdf")
    saved = call(ctx, "save_application_progress", session_id=sid)
    assert saved.ok
    prep = call(ctx, "prepare_submission", session_id=sid)
    assert prep.ok and prep.data and prep.data["dry_run_complete"] is True
    assert job_state(db, ready_job) is S.READY_FOR_SUBMISSION
    sub = call(ctx, "submit_application", session_id=sid)
    assert sub.error_code is ToolErrorCode.DRY_RUN_ONLY and "Nothing has been submitted" in (
        sub.message or ""
    )
    ver = call(ctx, "verify_submission", session_id=sid)
    assert ver.ok and ver.data and ver.data["submitted"] is False
    closed = call(ctx, "close_application_session", session_id=sid)
    assert closed.ok and job_state(db, ready_job) is S.READY_TO_APPLY
    assert site.submissions == []
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0


def test_dry_run_network_guard_blocks_any_post(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    """Even a forced click on the real submit button cannot send anything in a dry run."""
    ctx = tctx(db, exec_settings, exec_profile, browser)
    sid = (call(ctx, "open_application", job_id=ready_job).data or {})["session_id"]
    with pytest.raises(Exception, match="Dry-run sessions can never submit"):
        browser.click_submit(sid, "f0_s0")
    # Bypass PIE's own refusal on purpose (test-only access) to prove the network guard.
    browser._call(lambda: browser._live[sid].page.locator("form").evaluate("f => f.submit()"))
    browser._call(lambda: browser._live[sid].page.wait_for_timeout(500))
    assert site.submissions == []
    snap = browser.snapshot(sid)
    assert any(r["method"] == "POST" for r in snap.blocked_requests)
    call(ctx, "close_application_session", session_id=sid)


# --- blockers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "challenge"),
    [
        ("captcha", "CAPTCHA"),
        ("otp", "OTP"),
        ("login", "LOGIN_REQUIRED"),
        ("twofactor", "TWO_FACTOR"),
        ("identity", "IDENTITY_VERIFICATION"),
    ],
)
def test_challenges_pause_for_the_user(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    case: str,
    challenge: str,
) -> None:
    use_case(db, ready_job, site, case)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["blocked_needs_user"] is True and out["state"] == "WAITING_FOR_USER"
    kinds = {c["type"] for c in out["challenges"]}
    assert challenge in kinds, kinds
    assert out["user_action"] and "resume" in out["user_action"].lower()
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and session.filled_fields == []  # nothing typed
    assert any(s["label"] == "challenge" for s in session.screenshots)
    blocked = db.scalars(
        select(ExternalActionLog).where(ExternalActionLog.result == ExternalActionResult.BLOCKED)
    ).all()
    assert blocked and all(b.target_domain == "127.0.0.1" for b in blocked)
    assert site.submissions == []


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("unknown", "night shifts"),
        ("sponsorship", "sponsorship"),
        ("us_auth", "United States"),
        ("consent", "privacy policy"),
    ],
)
def test_unverified_questions_block_and_are_never_guessed(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    case: str,
    question: str,
) -> None:
    use_case(db, ready_job, site, case)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["blocked_needs_user"] is True
    assert any(question in m["question"] for m in out["missing_answers"]), out["missing_answers"]
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and session.filled_fields == []  # stopped before filling


def test_unknown_question_is_resumable_after_the_user_answers(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    entered: dict[str, str],
) -> None:
    use_case(db, ready_job, site, "unknown")
    ctx = ectx(db, exec_settings, exec_profile, browser)
    out = svc.run_execution(ctx, ready_job)
    missing = out["missing_answers"]
    assert len(missing) == 1
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert app is not None and app.requires_user_input
    svc.provide_answers(
        db,
        app,
        [
            {
                "question_key": missing[0]["question_key"],
                "question": missing[0]["question"],
                "answer": "No",
            }
        ],
    )
    resumed = svc.run_execution(ctx, ready_job)  # reopens the paused session
    assert resumed["session_id"] == out["session_id"]
    assert resumed["dry_run_complete"] is True and site.submissions == []
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None
    night = [f for f in session.filled_fields if f["key"] == missing[0]["question_key"]]
    assert night and "user" in night[0]["source"] and "value" not in night[0]
    assert entered[night[0]["field_id"]] == "No"  # on the page, not in PIE's records
    assert job_state(db, ready_job) is S.READY_TO_APPLY


def test_field_validation_error_stops_preparation(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "validation")  # phone must be 10 digits; ours is not
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["error"] == "The form is not ready"
    assert any("Phone" in p for p in out["details"]["problems"])
    assert site.submissions == []
    # A form PIE cannot complete is a failure, not a session left "in progress" (B3).
    assert_failed_cleanly(db, browser, exec_settings, out, ready_job, "prepare_submission")


def assert_failed_cleanly(
    db: Session,
    browser: Any,
    settings: Settings,
    out: dict[str, Any],
    job_id: uuid.UUID,
    stage: str,
) -> None:
    """REQUIREMENTS 12: screenshot, error logged with its step, job FAILED, page closed."""
    assert out["failed"] is True and out["state"] == "FAILED" and out["failed_stage"] == stage
    assert job_state(db, job_id) is S.FAILED
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and session.state is BrowserSessionState.FAILED
    assert not browser.is_alive(str(session.id))
    assert session.errors and session.errors[-1]["step"] == stage
    shot = next(s for s in session.screenshots if s["label"] == "failed")
    assert (settings.application_artifacts_dir / shot["file"]).is_file()
    app = db.scalar(select(Application).where(Application.job_id == job_id))
    assert app is not None and app.last_error
    failed = db.scalars(
        select(ApplicationEvent).where(ApplicationEvent.event_type == "execution_failed")
    ).all()
    assert failed and failed[-1].evidence and failed[-1].evidence["stage"] == stage
    logs = db.scalars(select(ExternalActionLog).where(ExternalActionLog.session_id == session.id))
    assert any(x.result is ExternalActionResult.FAILED for x in logs)


def test_page_without_a_form_fails_cleanly(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "noform")
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert "No application form" in out["error"]
    assert_failed_cleanly(db, browser, exec_settings, out, ready_job, "inspect_application_form")


def test_unexpected_redirect_fails_cleanly(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def elsewhere(session: BrowserSession, snap: Any) -> None:
        raise svc.ExecutionError("UNEXPECTED_PAGE", "The browser is on jobs.elsewhere.example")

    monkeypatch.setattr(svc, "_check_domain", elsewhere)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert "elsewhere" in out["error"] and out["details"]["code"] == "UNEXPECTED_PAGE"
    assert_failed_cleanly(db, browser, exec_settings, out, ready_job, "inspect_application_form")
    assert site.submissions == []


def test_crash_while_filling_fails_cleanly(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*_: Any) -> bool:
        raise RuntimeError("renderer crashed")

    monkeypatch.setattr(browser, "fill", crash)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    # Returned, not raised: the caller can go on with the next job.
    assert "renderer crashed" in out["error"]
    assert_failed_cleanly(db, browser, exec_settings, out, ready_job, "fill_application_field")


def test_agent_tool_error_on_the_wrong_site_fails_the_session(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    ctx = tctx(db, exec_settings, exec_profile, browser)
    sid = (call(ctx, "open_application", job_id=ready_job).data or {})["session_id"]
    session = db.get(BrowserSession, uuid.UUID(sid))
    assert session is not None
    session.target_url, session.target_domain = "https://jobs.elsewhere.example/apply", "x.example"
    db.commit()
    res = call(ctx, "inspect_application_form", session_id=sid)
    assert res.error_code is ToolErrorCode.UNEXPECTED_PAGE
    db.expire_all()
    assert session.state is BrowserSessionState.FAILED and job_state(db, ready_job) is S.FAILED
    assert not browser.is_alive(sid)
    assert any(s["label"] == "failed" for s in session.screenshots)


def test_missing_form_and_apply_button(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    ctx = tctx(db, exec_settings, exec_profile, browser)
    use_case(db, ready_job, site, "noform")
    sid = (call(ctx, "open_application", job_id=ready_job).data or {})["session_id"]
    miss = call(ctx, "inspect_application_form", session_id=sid)
    assert miss.error_code is ToolErrorCode.PRECONDITION_FAILED and "No application form" in (
        miss.message or ""
    )
    call(ctx, "close_application_session", session_id=sid)
    use_case(db, ready_job, site, "apply_button")
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["dry_run_complete"] is True  # clicked "Apply now", then the normal form


def test_navigation_failure_and_http_errors(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    job = db.get(Job, ready_job)
    assert job is not None
    job.application_url = site.base + "/jobs/does-not-exist"
    db.commit()
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["failed"] is True and "HTTP 404" in out["error"]
    assert job_state(db, ready_job) is S.FAILED


def test_unreachable_site_fails_cleanly(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
) -> None:
    job = db.get(Job, ready_job)
    assert job is not None
    job.application_url = "http://127.0.0.1:9/jobs/normal"  # nothing listens on port 9
    db.commit()
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["failed"] is True and "Could not open" in out["error"]
    assert job_state(db, ready_job) is S.FAILED


def test_browser_crash_recovery(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    ctx = tctx(db, exec_settings, exec_profile, browser)
    sid = (call(ctx, "open_application", job_id=ready_job).data or {})["session_id"]
    call(ctx, "inspect_application_form", session_id=sid)
    assert call(ctx, "fill_application_field", session_id=sid).ok
    assert call(ctx, "upload_application_resume", session_id=sid).ok
    session = db.get(BrowserSession, uuid.UUID(sid))
    assert session is not None
    before = {f["key"] for f in session.filled_fields}
    browser.crash_for_test(sid)
    assert not browser.is_alive(sid)
    # The next step reloads the page and rebuilds the recorded form state first.
    again = call(ctx, "fill_application_field", session_id=sid)
    assert again.ok, again.message
    db.expire_all()
    session = db.get(BrowserSession, uuid.UUID(sid))
    assert session is not None and browser.is_alive(sid)
    assert {f["key"] for f in session.filled_fields} == before
    assert session.uploaded_resume and session.uploaded_resume["status"] == "UPLOAD_UI_SUCCESS"
    recovery = [e["error"] for e in session.errors if e["step"] == "recovery"]
    assert recovery and f"restored {len(before)}/{len(before)}" in recovery[0]
    assert call(ctx, "prepare_submission", session_id=sid).ok
    call(ctx, "close_application_session", session_id=sid)
    assert site.submissions == []


# --- preconditions and gates --------------------------------------------------------------


def test_execution_needs_ready_to_apply_with_draft_and_resume(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    shortlisted_job_id: Any,
    site: ApplySite,
) -> None:
    ctx = tctx(db, exec_settings, exec_profile, browser)
    no_draft = call(ctx, "open_application", job_id=shortlisted_job_id)
    assert no_draft.error_code is ToolErrorCode.PRECONDITION_FAILED and "draft" in (
        no_draft.message or ""
    )
    internal = tctx(db, exec_settings, exec_profile, browser, ExecutionMode.INTERNAL_WRITES)
    call(internal, "create_application_draft", job_id=shortlisted_job_id)  # no tailored resume
    assert job_state(db, shortlisted_job_id) is S.SHORTLISTED
    shortlisted = call(ctx, "open_application", job_id=shortlisted_job_id)
    assert shortlisted.error_code is ToolErrorCode.PRECONDITION_FAILED
    assert "READY_TO_APPLY" in (shortlisted.message or "")
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert site.submissions == []


def test_external_tools_are_gated_by_mode(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    for mode, allowed, code in (
        (
            ExecutionMode.READ_ONLY,
            frozenset({ToolPermission.READ_ONLY}),
            ToolErrorCode.PERMISSION_DENIED,
        ),
        (
            ExecutionMode.INTERNAL_WRITES,
            frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE}),
            ToolErrorCode.PERMISSION_DENIED,
        ),
        (ExecutionMode.INTERNAL_WRITES, ALL, ToolErrorCode.EXTERNAL_NOT_ALLOWED),
        (LIVE, ALL, ToolErrorCode.DEVELOPMENT_MODE_BLOCKED),  # live requested in dev mode
    ):
        ctx = tctx(db, exec_settings, exec_profile, browser, mode)
        ctx.allowed = allowed
        assert call(ctx, "open_application", job_id=ready_job).error_code is code, mode
    refusals = db.scalars(
        select(ExternalActionLog).where(ExternalActionLog.result == ExternalActionResult.REFUSED)
    ).all()
    assert len(refusals) == 4 and all(r.tool == "open_application" for r in refusals)
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert job_state(db, ready_job) is S.READY_TO_APPLY and site.submissions == []


def test_dry_run_is_never_silently_upgraded_to_live(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    dry = tctx(db, live_settings, exec_profile, browser, DRY)
    sid = (call(dry, "open_application", job_id=ready_job).data or {})["session_id"]
    live = tctx(db, live_settings, exec_profile, browser, LIVE)
    assert (
        call(live, "open_application", job_id=ready_job).error_code is ToolErrorCode.MODE_MISMATCH
    )
    assert call(live, "submit_application", session_id=sid).error_code is ToolErrorCode.DRY_RUN_ONLY
    with pytest.raises(svc.ExecutionError, match="DRY RUN"):
        svc.confirm_live_submission(db, live_settings, uuid.UUID(sid), "yes")
    call(dry, "close_application_session", session_id=sid)
    assert site.submissions == []


# --- live (fixture site only; enabled explicitly in these tests) ---------------------------


def test_live_submission_requires_explicit_user_confirmation(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    sid = prepare_live(db, live_settings, exec_profile, browser, ready_job)
    agent = tctx(db, live_settings, exec_profile, browser, LIVE)
    refused = call(agent, "submit_application", session_id=sid)
    assert refused.error_code is ToolErrorCode.USER_CONFIRMATION_REQUIRED
    assert site.submissions == [] and job_state(db, ready_job) is S.READY_FOR_SUBMISSION
    confirm(db, live_settings, sid)
    done = call(agent, "submit_application", session_id=sid)
    assert done.ok and done.data and done.data["submitted"] is True, done
    evidence = done.data["evidence"]
    assert evidence["outcome"] == "CONFIRMED" and evidence["application_id"].startswith("APP-")
    assert len(site.submissions) == 1 and site.submissions[0]["has_pdf"]
    assert job_state(db, ready_job) is S.SUBMITTED
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert app is not None and app.applied_at is not None and app.status.value == "APPLIED"
    attempt = db.scalar(select(SubmissionAttempt))
    assert attempt is not None and attempt.outcome is SubmissionOutcome.CONFIRMED
    session = db.get(BrowserSession, sid)
    assert session is not None and session.state is B.SUBMITTED
    assert session.screenshots[-1]["label"] == "after_submit"
    # Never twice: the same session, a new session, or the agent retrying.
    again = call(agent, "submit_application", session_id=sid)
    assert again.error_code is ToolErrorCode.DUPLICATE_APPLICATION
    reopen = call(agent, "open_application", job_id=ready_job)
    assert reopen.error_code is ToolErrorCode.DUPLICATE_APPLICATION
    assert len(site.submissions) == 1


def test_confirmation_is_invalidated_by_a_new_prepare(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    sid = prepare_live(db, live_settings, exec_profile, browser, ready_job)
    confirm(db, live_settings, sid)
    session = db.get(BrowserSession, sid)
    assert session is not None
    session.state = B.FILLING  # the form changed; must be prepared again
    db.commit()
    agent = tctx(db, live_settings, exec_profile, browser, LIVE)
    assert call(agent, "prepare_submission", session_id=sid).ok
    stale = call(agent, "submit_application", session_id=sid)
    assert stale.error_code is ToolErrorCode.USER_CONFIRMATION_REQUIRED
    assert site.submissions == []
    call(agent, "close_application_session", session_id=sid)


def test_ambiguous_confirmation_is_not_a_submission(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "ambiguous")
    sid = prepare_live(db, live_settings, exec_profile, browser, ready_job)
    confirm(db, live_settings, sid)
    agent = tctx(db, live_settings, exec_profile, browser, LIVE)
    out = call(agent, "submit_application", session_id=sid)
    assert out.ok and out.data and out.data["submitted"] is False
    assert out.data["outcome"] == "AMBIGUOUS" and out.data["blocked_needs_user"] is True
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER
    assert len(site.submissions) == 1  # the click happened once...
    retry = call(agent, "submit_application", session_id=sid)
    assert retry.error_code is ToolErrorCode.DUPLICATE_APPLICATION  # ...and is never repeated
    assert len(site.submissions) == 1
    verify = call(agent, "verify_submission", session_id=sid)
    assert verify.ok and verify.data and verify.data["submitted"] is False
    # The user checks their email/portal and resolves it.
    user = ectx(db, live_settings, exec_profile, browser, live=True)
    svc.resolve_submission(user, sid, True, "Confirmation email received")
    assert job_state(db, ready_job) is S.SUBMITTED
    attempt = db.scalar(select(SubmissionAttempt))
    assert attempt is not None and attempt.outcome is SubmissionOutcome.USER_CONFIRMED
    assert attempt.evidence and attempt.evidence["user_resolution"] == "submitted"


def test_employer_error_is_failed_and_needs_the_user(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "error")
    sid = prepare_live(db, live_settings, exec_profile, browser, ready_job)
    confirm(db, live_settings, sid)
    out = call(
        tctx(db, live_settings, exec_profile, browser, LIVE), "submit_application", session_id=sid
    )
    assert out.ok and out.data and out.data["outcome"] == "FAILED" and not out.data["submitted"]
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER
    user = ectx(db, live_settings, exec_profile, browser, live=True)
    svc.resolve_submission(user, sid, False, "Employer page showed an error; not submitted")
    assert job_state(db, ready_job) is S.READY_TO_APPLY
    # A new, deliberate attempt is possible again after the user resolved it.
    use_case(db, ready_job, site, "normal")
    sid2 = prepare_live(db, live_settings, exec_profile, browser, ready_job)
    assert sid2 != sid


def test_live_cannot_run_unless_enabled(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    assert exec_settings.external_live_enabled is False
    with pytest.raises(svc.ExecutionError) as err:
        svc.open_application(ectx(db, exec_settings, exec_profile, browser, live=True), ready_job)
    assert err.value.code == "DEVELOPMENT_MODE_BLOCKED"
    production_without_flag = exec_settings.model_copy(update={"development_mode": False})
    with pytest.raises(svc.ExecutionError) as err:
        svc.open_application(
            ectx(db, production_without_flag, exec_profile, browser, live=True), ready_job
        )
    assert err.value.code == "LIVE_NOT_ENABLED"
    assert (
        db.scalar(select(func.count()).select_from(BrowserSession)) == 0 and site.submissions == []
    )


def test_duplicate_detection_across_jobs(db: Session, ready_job: uuid.UUID) -> None:
    job = db.get(Job, ready_job)
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert job is not None and app is not None
    assert svc.find_duplicate(db, job, app) is None
    twin = Job(
        source=job.source,
        source_job_id="other-id",
        title=job.title,
        company_id=job.company_id,
        url="https://elsewhere.test/1",
        pipeline_state=S.SUBMITTED,
    )
    db.add(twin)
    db.commit()
    assert "same company" in (svc.find_duplicate(db, job, app) or "")
    twin.title, twin.url = "Other role", job.url
    db.commit()
    assert "same application URL" in (svc.find_duplicate(db, job, app) or "")
    # (source, source_job_id) is unique in the database already; the canonical URL is not.
    twin.url, job.canonical_url = "https://elsewhere.test/2", "https://jobs.test/backend"
    twin.canonical_url = job.canonical_url
    db.commit()
    assert "same posting URL" in (svc.find_duplicate(db, job, app) or "")


# --- the Claude agent loop in external modes -----------------------------------------------


def _runner(
    session_factory: Any, settings: Settings, profile: UserProfile, browser: Any, claude: FakeClaude
) -> Any:
    from backend.services.agent_runner import AgentLimits, AgentRunner

    return AgentRunner(
        session_factory,
        claude,
        settings=settings,
        browser=browser,
        profile_loader=lambda: profile,
        limits=AgentLimits(backoff_seconds=0),
        sleep=lambda s: None,
    )


def _latest_session(session_factory: Any) -> str:
    with session_factory() as s:
        row = s.scalars(select(BrowserSession).order_by(BrowserSession.created_at.desc())).first()
        return str(row.id)


def test_agent_dry_run_workflow_stops_before_submit(
    db: Session,
    session_factory: Any,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    from backend.services.agent_runner import start_agent_run
    from database.enums import AgentRunStatus
    from database.models import AgentRun
    from tests.conftest import decision

    def sid(_: str) -> str:
        return _latest_session(session_factory)

    claude = FakeClaude(
        decision(tool="open_application", job_id=str(ready_job)),
        lambda p: decision(tool="inspect_application_form", session_id=sid(p)),
        lambda p: decision(tool="fill_application_field", session_id=sid(p), use_mapping=True),
        lambda p: decision(tool="upload_application_resume", session_id=sid(p)),
        lambda p: decision(tool="prepare_submission", session_id=sid(p)),
        lambda p: decision(tool="submit_application", session_id=sid(p)),  # must be refused
        lambda p: decision(tool="close_application_session", session_id=sid(p)),
        decision(action="complete", message="Dry run done. Nothing was submitted."),
    )
    run = start_agent_run(db, "Dry-run the application", mode="external_dry_run", max_iterations=12)
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    finished = db.get(AgentRun, run.id)
    assert finished is not None and finished.status is AgentRunStatus.COMPLETED, finished.summary
    assert '"dry_run":true' in claude.prompts[0]
    assert '"execution_mode":"external_dry_run"' in claude.prompts[0]
    assert "DRY_RUN_ONLY" in claude.prompts[6]  # Claude saw the refusal
    assert job_state(db, ready_job) is S.READY_TO_APPLY and site.submissions == []
    logs = db.scalars(select(ExternalActionLog).where(ExternalActionLog.run_id == run.id)).all()
    assert {log.tool for log in logs} >= {
        "open_application",
        "inspect_application_form",
        "prepare_submission",
        "submit_application",
    }
    assert all(log.mode == "DRY_RUN" for log in logs if log.session_id)
    assert finished.applications_submitted == 0
    # The stored agent actions and everything sent to Claude name answer keys, not answers.
    from database.models import AgentAction

    actions = db.scalars(select(AgentAction).where(AgentAction.run_id == run.id)).all()
    assert _leaks(exec_profile, {"actions": [a.output for a in actions]}) == []
    # Claude does see the profile facts (name, public links) by design; never the contact
    # details or the form answers (salary, notice period, work authorization...).
    identity = {"first_name", "last_name"} | {
        f"link {k}" for k in exec_profile.verified_profile.links
    }
    prompt_leaks = _leaks(exec_profile, {"prompts": claude.prompts})
    assert [k for k in prompt_leaks if k not in identity] == []


def test_agent_pauses_deterministically_on_a_blocker_and_resumes(
    db: Session,
    session_factory: Any,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    from backend.services.agent_runner import resume_agent_run, start_agent_run
    from database.enums import AgentRunStatus
    from database.models import AgentRun
    from tests.conftest import decision

    def sid(_: str) -> str:
        return _latest_session(session_factory)

    use_case(db, ready_job, site, "unknown")
    claude = FakeClaude(
        decision(tool="open_application", job_id=str(ready_job)),
        lambda p: decision(tool="inspect_application_form", session_id=sid(p)),
        decision(tool="fill_application_field", session_id="never-reached"),
    )
    run = start_agent_run(db, "Apply (dry run)", mode="external_dry_run", max_iterations=12)
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    paused = db.get(AgentRun, run.id)
    assert paused is not None and paused.status is AgentRunStatus.NEEDS_USER_INPUT
    assert "night shifts" in (paused.pending_user_input or "")
    assert len(claude.replies) == 1  # Claude was not asked again after the blocker
    # The user answers in the dashboard, then resumes the run.
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    session = db.get(BrowserSession, uuid.UUID(_latest_session(session_factory)))
    assert app is not None and session is not None
    q = session.missing_answers[0]
    svc.provide_answers(db, app, [{"question_key": q["question_key"], "answer": "No"}])
    resume_agent_run(db, run.id, message="Answered the night-shift question")
    claude.replies = [
        decision(tool="open_application", job_id=str(ready_job)),
        lambda p: decision(tool="inspect_application_form", session_id=sid(p)),
        lambda p: decision(tool="fill_application_field", session_id=sid(p)),
        lambda p: decision(tool="upload_application_resume", session_id=sid(p)),
        lambda p: decision(tool="prepare_submission", session_id=sid(p)),
        lambda p: decision(tool="close_application_session", session_id=sid(p)),
        decision(action="complete", message="Dry run complete."),
    ]
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    done = db.get(AgentRun, run.id)
    assert done is not None and done.status is AgentRunStatus.COMPLETED, done.summary
    assert job_state(db, ready_job) is S.READY_TO_APPLY and site.submissions == []


def test_agent_cannot_start_live_unless_enabled(db: Session) -> None:
    from backend.services.agent_runner import LiveModeDisabledError, start_agent_run

    with pytest.raises(LiveModeDisabledError, match="DEVELOPMENT_MODE_BLOCKED"):
        start_agent_run(
            db, "Submit", mode="external_live", live_enabled=True, development_mode=True
        )
    with pytest.raises(LiveModeDisabledError, match="LIVE_NOT_ENABLED"):
        start_agent_run(
            db, "Submit", mode="external_live", live_enabled=False, development_mode=False
        )
    with pytest.raises(ValueError):
        start_agent_run(db, "x", mode="external_yolo")


def test_live_agent_run_is_refused_if_setting_turned_off(
    db: Session,
    session_factory: Any,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
) -> None:
    from backend.services.agent_runner import start_agent_run
    from database.enums import AgentRunStatus
    from database.models import AgentRun

    run = start_agent_run(
        db, "Live", mode="external_live", live_enabled=True, development_mode=False
    )
    claude = FakeClaude()  # the runner's settings say DEVELOPMENT_MODE=true: refused
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    failed = db.get(AgentRun, run.id)
    assert failed is not None and failed.status is AgentRunStatus.FAILED
    assert claude.prompts == []  # not a single decision was requested


# --- API ------------------------------------------------------------------------------------


@pytest.fixture
def api(client: Any, exec_settings: Settings, exec_profile: UserProfile, browser: Any) -> Any:
    from backend.api import deps
    from backend.core.config import get_settings

    holder = {"settings": exec_settings}
    app = client.app
    app.dependency_overrides[get_settings] = lambda: holder["settings"]
    app.dependency_overrides[deps.profile_dep] = lambda: exec_profile
    app.dependency_overrides[deps.browser_dep] = lambda: browser
    app.dependency_overrides[deps.agent_claude_dep] = lambda: FakeClaude()
    return holder


def _app_id(db: Session, job_id: uuid.UUID) -> str:
    app = db.scalar(select(Application).where(Application.job_id == job_id))
    assert app is not None
    return str(app.id)


def test_api_dry_run_detail_and_screenshots(
    client: Any,
    api: Any,
    db: Session,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    app_id = _app_id(db, ready_job)
    listed = client.get("/api/applications").json()
    assert [a["id"] for a in listed] == [app_id]
    out = client.post(f"/api/applications/{app_id}/dry-run")
    assert out.status_code == 200, out.text
    assert out.json()["banner"] == "DRY RUN — Nothing has been submitted."
    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["external_live_enabled"] is False
    session = detail["sessions"][0]
    assert session["mode"] == "DRY_RUN" and session["dry_run_complete"] is True
    assert session["filled_fields"] and session["screenshots"]
    shot = client.get(f"/api/applications/{app_id}/sessions/{session['id']}/screenshots/0")
    assert shot.status_code == 200 and shot.content.startswith(b"\x89PNG")
    missing = client.get(f"/api/applications/{app_id}/sessions/{session['id']}/screenshots/99")
    assert missing.status_code == 404
    assert detail["external_actions"] and detail["timeline"]
    assert "You are about to submit an application to" in detail["confirmation_statement"]
    assert site.submissions == []


def test_api_live_is_forbidden_while_disabled(
    client: Any,
    api: Any,
    db: Session,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    app_id = _app_id(db, ready_job)
    assert client.post(f"/api/applications/{app_id}/live/prepare").status_code == 403
    run = client.post("/api/agent/runs", json={"task": "Apply", "mode": "external_live"})
    assert run.status_code == 403
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert site.submissions == []


def test_api_live_flow_needs_the_exact_confirmation(
    client: Any,
    api: Any,
    live_settings: Settings,
    db: Session,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    api["settings"] = live_settings
    app_id = _app_id(db, ready_job)
    prepared = client.post(f"/api/applications/{app_id}/live/prepare").json()
    assert prepared["state"] == "READY_FOR_SUBMISSION"
    assert prepared["banner"] == "LIVE EXTERNAL ACTION"
    sid = prepared["session_id"]
    base = f"/api/applications/{app_id}/sessions/{sid}"
    early = client.post(f"{base}/submit")
    assert early.status_code == 428 and site.submissions == []
    wrong = client.post(f"{base}/confirm-live", json={"acknowledged_statement": "sure, go ahead"})
    assert wrong.status_code == 422
    statement = client.get(f"/api/applications/{app_id}").json()["confirmation_statement"]
    ok = client.post(f"{base}/confirm-live", json={"acknowledged_statement": statement})
    assert ok.status_code == 200
    done = client.post(f"{base}/submit").json()
    assert done["submitted"] is True and len(site.submissions) == 1
    assert client.post(f"{base}/submit").status_code == 409  # never twice


def test_api_answers_unblock_a_dry_run(
    client: Any,
    api: Any,
    db: Session,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "unknown")
    app_id = _app_id(db, ready_job)
    blocked = client.post(f"/api/applications/{app_id}/dry-run").json()
    assert blocked["blocked_needs_user"] is True
    q = blocked["missing_answers"][0]
    answer = {"question_key": q["question_key"], "question": q["question"], "answer": "No"}
    saved = client.post(f"/api/applications/{app_id}/answers", json={"answers": [answer]})
    assert saved.json()["saved"] == 1
    again = client.post(f"/api/applications/{app_id}/dry-run").json()
    assert again["dry_run_complete"] is True and site.submissions == []


# --- security -------------------------------------------------------------------------------


def test_no_tool_accepts_code_urls_selectors_paths_or_values() -> None:
    forbidden = {
        "url",
        "script",
        "js",
        "javascript",
        "selector",
        "command",
        "shell",
        "code",
        "eval",
        "path",
        "file",
        "value",
        "password",
        "cookie",
        "cookies",
        "token",
        "html",
        "expression",
    }
    for tool in REG.tools():
        schema = tool.input_schema()
        names: set[str] = set(schema.get("properties", {}))
        for sub in schema.get("$defs", {}).values():
            names |= set(sub.get("properties", {}))
        assert not names & forbidden, (tool.name, names & forbidden)


def test_browser_manager_exposes_only_fixed_operations() -> None:
    public = {n for n in dir(BrowserManager) if not n.startswith("_")}
    assert public == {
        "start",  # launch only: no page, no caller-supplied code
        "open",
        "is_alive",
        "snapshot",
        "click_apply",
        "fill",
        "upload",
        "click_submit",
        "combobox_options",
        "combobox_select",
        "combobox_close",
        "screenshot",
        "close",
        "shutdown",
        "crash_for_test",
    }


def test_no_credentials_are_stored_or_logged(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    from database.base import Base

    columns = {c.name for t in Base.metadata.tables.values() for c in t.columns}
    risky = {
        c for c in columns if any(w in c for w in ("password", "cookie", "token", "secret", "otp"))
    }
    assert risky == set()
    use_case(db, ready_job, site, "login")
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    svc.close_session(ectx(db, exec_settings, exec_profile, browser), uuid.UUID(out["session_id"]))
    job = db.get(Job, ready_job)
    assert job is not None
    job.pipeline_state = S.READY_TO_APPLY  # the user dealt with the login elsewhere
    job.application_url = site.url("normal") + "?token=SECRET123&otp=4242"
    db.commit()
    svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    sessions = db.scalars(select(BrowserSession)).all()
    dump = " ".join(
        str(v)
        for s in sessions
        for v in (s.current_url, s.detected_fields, s.filled_fields, s.errors)
    ) + " ".join(log.summary + (log.error or "") for log in db.scalars(select(ExternalActionLog)))
    assert "SECRET123" not in dump and "4242" not in dump
    assert "password" not in {f["key"] for s in sessions for f in s.filled_fields}


# =====================================================================================
# Hardening: development-mode gate, dropdowns, classification, recovery, upload states
# =====================================================================================


class SpyBrowser:
    """Records every browser operation, delegating to a real BrowserManager."""

    def __init__(self, inner: BrowserManager) -> None:
        self.inner = inner
        self.headless = inner.headless
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return target(*args, **kwargs)

        return wrapper


def no_sessions_no_submissions(db: Session, site: ApplySite) -> None:
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0
    assert site.submissions == []


# --- 1. development-mode safety gate -----------------------------------------------------


def test_development_mode_blocks_live_even_when_the_live_flag_is_on(
    db: Session,
    dev_live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    from backend.services.agent_runner import LiveModeDisabledError, start_agent_run

    s = dev_live_settings
    assert s.development_mode is True and s.external_live_enabled is True
    spy = SpyBrowser(browser)
    for fn in (svc.open_application, svc.run_execution):
        with pytest.raises(svc.ExecutionError) as err:
            fn(ectx(db, s, exec_profile, spy, live=True), ready_job)
        assert err.value.code == "DEVELOPMENT_MODE_BLOCKED"
    gated = call(tctx(db, s, exec_profile, spy, LIVE), "open_application", job_id=ready_job)
    assert gated.error_code is ToolErrorCode.DEVELOPMENT_MODE_BLOCKED
    with pytest.raises(LiveModeDisabledError, match="DEVELOPMENT_MODE_BLOCKED"):
        start_agent_run(db, "Apply", mode="external_live", live_enabled=True, development_mode=True)
    assert spy.calls == []  # not a single browser operation ran
    assert job_state(db, ready_job) is S.READY_TO_APPLY
    no_sessions_no_submissions(db, site)


def test_development_mode_blocks_submit_before_any_browser_code(
    db: Session,
    live_settings: Settings,
    dev_live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    sid = prepare_live(db, live_settings, exec_profile, browser, ready_job)  # production config
    confirm(db, live_settings, sid)
    spy = SpyBrowser(browser)
    out = call(
        tctx(db, dev_live_settings, exec_profile, spy, LIVE), "submit_application", session_id=sid
    )
    assert out.error_code is ToolErrorCode.DEVELOPMENT_MODE_BLOCKED
    with pytest.raises(svc.ExecutionError) as err:
        svc.submit(ectx(db, dev_live_settings, exec_profile, spy, live=True), sid)
    assert err.value.code == "DEVELOPMENT_MODE_BLOCKED"
    with pytest.raises(svc.ExecutionError, match="DEVELOPMENT_MODE"):
        svc.confirm_live_submission(db, dev_live_settings, sid, "yes")
    assert spy.calls == [] and site.submissions == []
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0
    assert job_state(db, ready_job) is S.READY_FOR_SUBMISSION
    svc.close_session(ectx(db, live_settings, exec_profile, browser, live=True), sid)


def test_live_needs_a_visible_browser(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    headless = live_settings.model_copy(update={"browser_headless": True})
    with pytest.raises(svc.ExecutionError) as err:
        svc.run_execution(ectx(db, headless, exec_profile, browser, live=True), ready_job)
    assert err.value.code == "VISIBLE_BROWSER_REQUIRED"
    no_sessions_no_submissions(db, site)


def test_api_shows_and_enforces_the_development_mode_block(
    client: Any,
    api: Any,
    dev_live_settings: Settings,
    db: Session,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    api["settings"] = dev_live_settings
    mode = client.get("/api/applications/mode").json()
    assert mode == {
        "development_mode": True,
        "external_live_configured": True,
        "external_live_enabled": False,
        "live_block_reason": "DEVELOPMENT_MODE_BLOCKED",
        "browser_headless": False,
        "default_mode": "external_dry_run",
    }
    app_id = _app_id(db, ready_job)
    res = client.post(f"/api/applications/{app_id}/live/prepare")
    assert res.status_code == 403 and res.json()["code"] == "DEVELOPMENT_MODE_BLOCKED"
    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["external_live_enabled"] is False and detail["development_mode"] is True
    status = client.get("/api/agent/status").json()
    assert status["development_mode"] is True and status["external_live_enabled"] is False
    assert status["external_live_configured"] is True
    run = client.post("/api/agent/runs", json={"task": "Apply", "mode": "external_live"})
    assert run.status_code == 403 and "DEVELOPMENT_MODE_BLOCKED" in run.json()["detail"]
    no_sessions_no_submissions(db, site)


# --- 2. custom dropdowns ---------------------------------------------------------------------


def dry(
    db: Session,
    settings: Settings,
    profile: UserProfile,
    browser: Any,
    job_id: uuid.UUID,
    site: ApplySite,
    case: str,
) -> tuple[dict[str, Any], BrowserSession]:
    use_case(db, job_id, site, case)
    out = svc.run_execution(ectx(db, settings, profile, browser), job_id)
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None
    return out, session


def filled(session: BrowserSession, key: str) -> dict[str, Any]:
    return next(f for f in session.filled_fields if f["key"] == key)


def shown(entered: dict[str, str], session: BrowserSession, key: str) -> str:
    """What PIE put on the page for a filled field (PIE itself does not store it)."""
    field_id = filled(session, key)["field_id"]
    value = entered[field_id]
    if value.startswith("option "):
        plan = next(f for f in session.detected_fields if f["field_id"] == field_id)
        return str(plan["options"][int(value.split()[1])])
    return value


def test_act_select_is_opened_selected_and_verified(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    entered: dict[str, str],
) -> None:
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, "actselect")
    assert out["dry_run_complete"] is True, out
    auth = filled(session, "work_authorization")
    assert auth["method"] == "dropdown" and shown(entered, session, "work_authorization") == "Yes"
    field = next(f for f in session.detected_fields if f["key"] == "work_authorization")
    assert field["kind"] == "combobox" and field["category"] == "VERIFIED_AUTOFILL"
    assert site.submissions == []


def test_searchable_act_select_searches_for_the_verified_value(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    entered: dict[str, str],
) -> None:
    out, session = dry(
        db, exec_settings, exec_profile, browser, ready_job, site, "actselect_search"
    )
    assert out["dry_run_complete"] is True, out
    notice = filled(session, "notice_period")
    assert (
        notice["method"] == "dropdown" and shown(entered, session, "notice_period") == "Immediate"
    )


@pytest.mark.parametrize("case", ["actselect_missing", "actselect_ambiguous"])
def test_dropdown_without_a_clear_option_blocks_and_selects_nothing(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
    case: str,
) -> None:
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, case)
    assert out["blocked_needs_user"] is True
    missing = [m for m in session.missing_answers if m["question_key"] == "work_authorization"]
    assert missing and missing[0]["category"] == "AMBIGUOUS"
    assert "work_authorization" not in {f["key"] for f in session.filled_fields}
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER
    snap = browser.snapshot(str(session.id))  # the page shows no selection
    combo = next(f for f in snap.fields if f["kind"] == "combobox")
    assert combo["selected_label"] == ""
    assert site.submissions == []


@pytest.mark.parametrize(
    ("case", "method"),
    [("actselect_selected", "dropdown (already set)"), ("select_selected", "already selected")],
)
def test_already_selected_values_are_kept(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    case: str,
    method: str,
) -> None:
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, case)
    assert out["dry_run_complete"] is True, out
    assert filled(session, "work_authorization")["method"] == method


def test_normal_select_still_works(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    entered: dict[str, str],
) -> None:
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, "normal")
    auth = filled(session, "work_authorization")
    assert auth["method"] == "direct" and shown(entered, session, "work_authorization") == "Yes"


# --- 3/4. answers and classification on a real form -----------------------------------------


def test_every_field_is_classified_and_only_autofill_is_filled(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    _, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, "normal")
    categories = {f["label"]: f["category"] for f in session.detected_fields}
    assert categories["Email"] == "VERIFIED_AUTOFILL"
    assert categories["Gender"] == "SENSITIVE_USER_REQUIRED"
    assert categories["How did you hear about us?"] == "UNKNOWN"
    autofill = {
        f["field_id"] for f in session.detected_fields if f["category"] == "VERIFIED_AUTOFILL"
    }
    assert {f["field_id"] for f in session.filled_fields} <= autofill


@pytest.mark.parametrize(
    ("case", "category"),
    [
        ("unknown", "UNKNOWN"),
        ("sponsorship", "SENSITIVE_USER_REQUIRED"),
        ("us_auth", "SENSITIVE_USER_REQUIRED"),
        ("consent", "SENSITIVE_USER_REQUIRED"),
    ],
)
def test_unknown_and_sensitive_questions_stop_with_their_category(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    case: str,
    category: str,
) -> None:
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, case)
    assert out["blocked_needs_user"] is True
    assert category in {m["category"] for m in session.missing_answers}
    assert session.filled_fields == []


def test_contradictory_name_sources_stop_the_form(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    facts = exec_profile.verified_profile.model_copy(
        update={
            "verified_answers": {
                **exec_profile.verified_profile.verified_answers,
                "first_name": "Someone",
            }
        }
    )
    conflicted = exec_profile.model_copy(update={"verified_profile": facts})
    out, session = dry(db, exec_settings, conflicted, browser, ready_job, site, "normal")
    assert out["blocked_needs_user"] is True
    first = next(m for m in session.missing_answers if m["question_key"] == "first_name")
    assert first["category"] == "AMBIGUOUS"


# --- 7. challenge false positives --------------------------------------------------------------


def test_job_description_mentioning_2fa_otp_captcha_does_not_block(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    out, session = dry(
        db, exec_settings, exec_profile, browser, ready_job, site, "description_words"
    )
    assert out["dry_run_complete"] is True, out
    assert session.challenges == [] and site.submissions == []


# --- 6. recovery after a backend restart --------------------------------------------------------


def test_backend_restart_reloads_the_page_and_rebuilds_the_form(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    before_restart = BrowserManager(headless=True, timeout_ms=15_000)
    ctx = tctx(db, exec_settings, exec_profile, before_restart)
    sid = (call(ctx, "open_application", job_id=ready_job).data or {})["session_id"]
    call(ctx, "inspect_application_form", session_id=sid)
    assert call(ctx, "fill_application_field", session_id=sid).ok
    assert call(ctx, "upload_application_resume", session_id=sid).ok
    session = db.get(BrowserSession, uuid.UUID(sid))
    assert session is not None
    keys = {f["key"] for f in session.filled_fields}
    before_restart.shutdown()  # the backend process ends: every page is gone

    after_restart = BrowserManager(headless=True, timeout_ms=15_000)  # a new process
    try:
        assert not after_restart.is_alive(sid)
        ctx = tctx(db, exec_settings, exec_profile, after_restart)
        prep = call(ctx, "prepare_submission", session_id=sid)
        assert prep.ok and prep.data and prep.data["ready"] is True, prep
        db.expire_all()
        session = db.get(BrowserSession, uuid.UUID(sid))
        assert session is not None and {f["key"] for f in session.filled_fields} == keys
        assert any("restored" in e["error"] for e in session.errors if e["step"] == "recovery")
        call(ctx, "close_application_session", session_id=sid)
    finally:
        after_restart.shutdown()
    assert site.submissions == []


def test_paused_session_resumes_after_a_restart(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "unknown")
    first = BrowserManager(headless=True, timeout_ms=15_000)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, first), ready_job)
    assert out["blocked_needs_user"] is True
    q = out["missing_answers"][0]
    first.shutdown()
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert app is not None
    svc.provide_answers(db, app, [{"question_key": q["question_key"], "answer": "No"}])
    second = BrowserManager(headless=True, timeout_ms=15_000)
    try:
        resumed = svc.run_execution(ectx(db, exec_settings, exec_profile, second), ready_job)
        assert resumed["session_id"] == out["session_id"]
        assert resumed["dry_run_complete"] is True, resumed
    finally:
        second.shutdown()
    assert site.submissions == []


def test_paused_page_is_kept_while_the_backend_runs(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "unknown")
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["blocked_needs_user"] is True and browser.is_alive(out["session_id"])
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert app is not None
    q = out["missing_answers"][0]
    svc.provide_answers(db, app, [{"question_key": q["question_key"], "answer": "No"}])
    resumed = svc.open_application(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert resumed["resumed"] is True and resumed["page_kept"] is True


# --- 8. dry-run upload states ---------------------------------------------------------------------


def test_dry_run_upload_states(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    _, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, "normal")
    assert session.uploaded_resume and session.uploaded_resume["status"] == "UPLOAD_UI_SUCCESS"
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, "async_upload")
    upload = session.uploaded_resume or {}
    assert upload["status"] == "UPLOAD_REQUEST_BLOCKED_BY_DRY_RUN_GUARD"
    assert "did NOT receive" in upload["detail"] and site.uploads == []
    assert out["report"]["upload_status"] == "UPLOAD_REQUEST_BLOCKED_BY_DRY_RUN_GUARD"
    out, session = dry(db, exec_settings, exec_profile, browser, ready_job, site, "upload_error")
    assert (session.uploaded_resume or {})["status"] == "UPLOAD_FAILED"
    assert out["error"] == "The form is not ready"
    assert any("resume" in p for p in out["details"]["problems"])
    assert site.submissions == []


def test_live_upload_success_is_reported_as_such(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    sid = prepare_live(db, live_settings, exec_profile, browser, ready_job)
    session = db.get(BrowserSession, sid)
    assert session is not None and (session.uploaded_resume or {})["status"] == "UPLOAD_SUCCESS"
    svc.close_session(ectx(db, live_settings, exec_profile, browser, live=True), sid)
    assert site.submissions == []


# --- 9. submission safety ----------------------------------------------------------------


@pytest.mark.parametrize("case", ["unknown", "captcha", "otp"])
def test_blocked_forms_can_never_be_submitted_even_live(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    case: str,
) -> None:
    use_case(db, ready_job, site, case)
    out = svc.run_execution(ectx(db, live_settings, exec_profile, browser, live=True), ready_job)
    assert out["blocked_needs_user"] is True
    sid = uuid.UUID(out["session_id"])
    with pytest.raises(svc.ExecutionError):
        svc.confirm_live_submission(db, live_settings, sid, "yes")
    refused = call(
        tctx(db, live_settings, exec_profile, browser, LIVE), "submit_application", session_id=sid
    )
    assert refused.error_code is ToolErrorCode.PRECONDITION_FAILED
    assert site.submissions == []
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER


# --- B3 hardening: dead pages, hung cleanup, leaks, end of agent runs, browser start -------


def _contexts(browser: BrowserManager) -> int:
    """Open browser contexts (one per session): a leak shows up here."""
    return browser._call(lambda: len(browser._browser.contexts) if browser._browser else 0)


def test_really_dead_page_still_ends_failed(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real page is closed underneath PIE while it fills; the screenshot then fails too."""
    real_fill = browser.fill

    def fill_then_die(sid: str, *args: Any) -> bool:
        browser.crash_for_test(sid)  # a real dead page, not a raised exception
        return real_fill(sid, *args)

    before = _contexts(browser)
    monkeypatch.setattr(browser, "fill", fill_then_die)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["failed"] is True and job_state(db, ready_job) is S.FAILED
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and session.state is BrowserSessionState.FAILED
    assert not any(s["label"] == "failed" for s in session.screenshots)  # nothing to photograph
    assert any("screenshot failed" in e["error"] for e in session.errors)
    assert not browser.is_alive(str(session.id)) and _contexts(browser) == before


def test_cleanup_finishes_when_screenshot_and_close_fail(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_case(db, ready_job, site, "noform")
    real_close = browser.close

    def hung_screenshot(*_: Any, **__: Any) -> None:
        raise TimeoutError("page did not respond")

    def broken_close(sid: str) -> None:
        real_close(sid)  # (free the real page so the module's browser stays clean)
        raise RuntimeError("close failed")

    monkeypatch.setattr(browser, "screenshot", hung_screenshot)
    monkeypatch.setattr(browser, "close", broken_close)
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["failed"] is True and job_state(db, ready_job) is S.FAILED
    session = db.get(BrowserSession, uuid.UUID(out["session_id"]))
    assert session is not None and session.state is BrowserSessionState.FAILED
    errors = " ".join(e["error"] for e in session.errors)
    assert "screenshot failed: TimeoutError" in errors and "closing the page failed" in errors


def test_repeated_failures_leak_no_browser_contexts(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "noform")
    before = _contexts(browser)
    job = db.get(Job, ready_job)
    assert job is not None
    for _ in range(3):
        out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
        assert out["failed"] is True
        job.pipeline_state = S.READY_TO_APPLY  # as a manual retry would
        db.commit()
    assert _contexts(browser) == before


def test_browser_that_cannot_start_changes_nothing(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_browser() -> None:
        raise BrowserError("UNAVAILABLE", "No usable browser could be launched")

    monkeypatch.setattr(browser, "start", no_browser)
    with pytest.raises(svc.ExecutionError) as err:
        svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert err.value.code == "BROWSER_ERROR" and "could not be started" in err.value.message
    # Nothing started: no session, and the job is still ready (not failed, not in progress).
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert job_state(db, ready_job) is S.READY_TO_APPLY


def test_agent_run_that_ends_mid_application_fails_the_session(
    db: Session,
    session_factory: Any,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    """A recoverable stop leaves the page open for the agent; if the run then hits its step
    limit, the page must not stay "in progress"."""
    from backend.services.agent_runner import start_agent_run
    from database.enums import AgentRunStatus
    from database.models import AgentRun
    from tests.conftest import decision

    use_case(db, ready_job, site, "apply_button")

    def sid(_: str) -> str:
        return _latest_session(session_factory)

    claude = FakeClaude(
        decision(tool="open_application", job_id=str(ready_job)),
        lambda p: decision(tool="inspect_application_form", session_id=sid(p)),
    )
    before = _contexts(browser)
    run = start_agent_run(db, "Dry-run the application", mode="external_dry_run", max_iterations=2)
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    ended = db.get(AgentRun, run.id)
    assert ended is not None and ended.status is AgentRunStatus.FAILED  # iteration limit
    session = db.get(BrowserSession, uuid.UUID(_latest_session(session_factory)))
    assert session is not None and session.state is BrowserSessionState.FAILED
    assert session.errors[-1]["step"] == "end_of_agent_run"
    assert job_state(db, ready_job) is S.FAILED
    assert not browser.is_alive(str(session.id)) and _contexts(browser) == before


def test_agent_run_that_completes_after_a_dry_run_closes_it_normally(
    db: Session,
    session_factory: Any,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    from backend.services.agent_runner import start_agent_run
    from tests.conftest import decision

    def sid(_: str) -> str:
        return _latest_session(session_factory)

    claude = FakeClaude(
        decision(tool="open_application", job_id=str(ready_job)),
        lambda p: decision(tool="inspect_application_form", session_id=sid(p)),
        lambda p: decision(tool="fill_application_field", session_id=sid(p), use_mapping=True),
        lambda p: decision(tool="upload_application_resume", session_id=sid(p)),
        lambda p: decision(tool="prepare_submission", session_id=sid(p)),
        decision(action="complete", message="Prepared; forgot to close."),
    )
    run = start_agent_run(db, "Dry-run the application", mode="external_dry_run", max_iterations=12)
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    session = db.get(BrowserSession, uuid.UUID(_latest_session(session_factory)))
    assert session is not None and session.state is BrowserSessionState.CLOSED  # a success
    assert job_state(db, ready_job) is S.READY_TO_APPLY and site.submissions == []


# --- B4: failed jobs are never re-scored automatically; the user retries them -------------


@pytest.fixture
def failed_job(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> uuid.UUID:
    """A job whose application really failed in the browser (a page without a form)."""
    use_case(db, ready_job, site, "noform")
    job = db.get(Job, ready_job)
    assert job is not None
    job.posted_at = utcnow()  # fresh: exactly the jobs automatic analysis looks at
    db.commit()
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["failed"] is True and job_state(db, ready_job) is S.FAILED
    return ready_job


def test_failed_jobs_are_not_picked_for_automatic_analysis(
    db: Session, failed_job: uuid.UUID
) -> None:
    from backend.services.analysis import select_jobs_for_batch, select_jobs_for_daily

    now = utcnow()
    batch = db.scalars(select_jobs_for_batch(now, 48, True, True, 100)).all()
    assert failed_job not in {j.id for j in batch}
    assert failed_job not in select_jobs_for_daily(db, now, 48, 100)


def test_failed_job_cannot_be_reanalyzed_by_the_agent_or_the_service(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    failed_job: uuid.UUID,
) -> None:
    from backend.services.analysis import NotAnalyzableError, analyze_job

    ctx = tctx(db, exec_settings, exec_profile, browser, ExecutionMode.INTERNAL_WRITES)
    res = call(ctx, "analyze_job", job_id=failed_job)
    assert not res.ok and job_state(db, failed_job) is S.FAILED
    job = db.get(Job, failed_job)
    assert job is not None
    with pytest.raises(NotAnalyzableError):
        analyze_job(db, job, None, None)  # type: ignore[arg-type]  # refused before use
    assert job_state(db, failed_job) is S.FAILED


def test_failed_jobs_are_listed_with_stage_error_and_screenshot(
    db: Session, exec_settings: Settings, failed_job: uuid.UUID
) -> None:
    from backend.services.failed_jobs import list_failed_jobs

    [item] = list_failed_jobs(db)
    assert item.job_id == str(failed_job) and item.stage == "inspect_application_form"
    assert item.error and "No application form" in item.error
    assert item.screenshot and (exec_settings.application_artifacts_dir / item.screenshot).is_file()
    assert item.retries == 0 and item.failed_at is not None


def test_user_retry_puts_the_job_back_and_it_can_run_again(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    failed_job: uuid.UUID,
    site: ApplySite,
) -> None:
    from backend.services.failed_jobs import RetryError, list_failed_jobs, retry_failed_job

    job = retry_failed_job(db, exec_settings, exec_profile, failed_job)
    assert job.pipeline_state is S.READY_TO_APPLY  # draft, resume and answers still complete
    events = db.scalars(
        select(ApplicationEvent).where(ApplicationEvent.event_type == "retried_by_user")
    ).all()
    assert len(events) == 1 and events[0].source is EventSource.USER
    assert list_failed_jobs(db) == []
    with pytest.raises(RetryError, match="only FAILED jobs"):
        retry_failed_job(db, exec_settings, exec_profile, failed_job)
    # A retry only re-queues: running it again is a separate, explicit step.
    use_case(db, failed_job, site, "normal")
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), failed_job)
    assert out["dry_run_complete"] is True and site.submissions == []


def test_retry_api_and_cli(
    db: Session,
    session_factory: Any,
    client: Any,
    failed_job: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from backend.scripts import jobs as jobs_cli

    listed = client.get("/api/jobs/failed").json()
    assert listed["total"] == 1 and listed["items"][0]["stage"] == "inspect_application_form"
    assert client.post(f"/api/jobs/{uuid.uuid4()}/retry").status_code == 404

    monkeypatch.setattr(jobs_cli, "get_session_factory", lambda: session_factory)
    assert jobs_cli.main(["failed"]) == 0
    assert str(failed_job) in capsys.readouterr().out
    res = client.post(f"/api/jobs/{failed_job}/retry")
    assert res.status_code == 200 and res.json()["status"] != "FAILED"
    assert client.post(f"/api/jobs/{failed_job}/retry").status_code == 409  # not failed now
    assert jobs_cli.main(["retry", str(failed_job)]) == 1  # the CLI refuses the same way
    assert "only FAILED jobs" in capsys.readouterr().out


# --- B5: a resume linked to an application in progress or submitted never changes ---------


def _tailor(
    db: Session, settings: Settings, profile: UserProfile, browser: Any, job_id: uuid.UUID
) -> tuple[ToolResult, FakeClaude]:
    claude = FakeClaude(good_resume())
    ctx = tctx(db, settings, profile, browser, ExecutionMode.INTERNAL_WRITES)
    ctx.claude_factory = lambda: claude
    return call(ctx, "create_tailored_resume", job_id=job_id, force_new=True), claude


def _linked(db: Session, job_id: uuid.UUID) -> uuid.UUID | None:
    db.expire_all()
    app = db.scalar(select(Application).where(Application.job_id == job_id))
    assert app is not None
    return app.resume_version_id


def _versions(db: Session) -> int:
    from database.models import ResumeVersion

    return int(db.scalar(select(func.count()).select_from(ResumeVersion)) or 0)


def _assert_locked(
    db: Session, settings: Settings, profile: UserProfile, browser: Any, job_id: uuid.UUID
) -> None:
    before, versions = _linked(db, job_id), _versions(db)
    res, claude = _tailor(db, settings, profile, browser, job_id)
    assert not res.ok and "locked" in (res.message or ""), res
    assert claude.prompts == []  # refused before any Claude call (no usage spent)
    assert _linked(db, job_id) == before and _versions(db) == versions


def test_resume_is_locked_while_the_application_is_in_progress(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
) -> None:
    ctx = tctx(db, exec_settings, exec_profile, browser)
    assert call(ctx, "open_application", job_id=ready_job).ok
    assert job_state(db, ready_job) is S.APPLICATION_IN_PROGRESS
    _assert_locked(db, exec_settings, exec_profile, browser, ready_job)


def test_resume_is_locked_while_a_session_is_paused_for_the_user(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    use_case(db, ready_job, site, "unknown")  # a question PIE may not answer: pauses
    out = svc.run_execution(ectx(db, exec_settings, exec_profile, browser), ready_job)
    assert out["blocked_needs_user"] is True
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER  # not an "in progress" state...
    _assert_locked(db, exec_settings, exec_profile, browser, ready_job)  # ...but still locked


def test_resume_is_locked_once_submitted_or_attempted(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
) -> None:
    from database.enums import ApplicationStatus

    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    job = db.get(Job, ready_job)
    assert app is not None and job is not None
    app.status = ApplicationStatus.APPLIED  # e.g. confirmed by email later
    db.commit()
    _assert_locked(db, exec_settings, exec_profile, browser, ready_job)
    app.status = ApplicationStatus.DRAFT
    db.add(
        SubmissionAttempt(
            idempotency_key="k" * 64,
            application_id=app.id,
            outcome=SubmissionOutcome.AMBIGUOUS,
        )
    )
    db.commit()
    _assert_locked(db, exec_settings, exec_profile, browser, ready_job)


def test_resume_can_change_before_applying_and_after_a_clean_failure(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    failed_job: uuid.UUID,
) -> None:
    """A failed dry run sent nothing, so a new resume may be linked before a retry."""
    before = _linked(db, failed_job)
    res, _ = _tailor(db, exec_settings, exec_profile, browser, failed_job)
    assert res.ok, res.message
    assert _linked(db, failed_job) not in (None, before)


def test_link_itself_refuses_a_locked_application(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
) -> None:
    """Defense in depth: even a caller that skipped the first check cannot swap it."""
    from backend.services import tailoring
    from database.models import ResumeVersion

    ctx = tctx(db, exec_settings, exec_profile, browser)
    assert call(ctx, "open_application", job_id=ready_job).ok
    job = db.get(Job, ready_job)
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert job is not None and app is not None
    other = db.scalars(
        select(ResumeVersion).where(ResumeVersion.id != app.resume_version_id)
    ).first()
    assert other is not None  # the master version
    with pytest.raises(tailoring.TailoringError, match="locked"):
        tailoring._link(db, exec_settings, exec_profile, job, app, other, "agent", None)


def test_failed_pdf_leaves_no_half_made_version(
    db: Session,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
    ready_job: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import tailoring

    def broken_pdf(*_: Any) -> bytes:
        raise RuntimeError("font missing")

    monkeypatch.setattr(tailoring, "render_pdf", broken_pdf)
    folder = exec_settings.resumes_dir / "tailored" / f"job-{ready_job}"
    files_before = sorted(p.name for p in folder.iterdir())
    versions, linked = _versions(db), _linked(db, ready_job)
    res, _ = _tailor(db, exec_settings, exec_profile, browser, ready_job)
    assert not res.ok
    assert sorted(p.name for p in folder.iterdir()) == files_before  # no orphan .md
    assert _versions(db) == versions and _linked(db, ready_job) == linked


def test_agent_action_log_never_contains_answer_values(
    db: Session,
    session_factory: Any,
    exec_settings: Settings,
    exec_profile: UserProfile,
    browser: BrowserManager,
) -> None:
    """B6: the agent asks which answers exist; what is stored and sent is keys only."""
    from backend.services.agent_runner import start_agent_run
    from database.enums import AgentRunStatus
    from database.models import AgentAction, AgentRun
    from tests.conftest import decision

    claude = FakeClaude(
        decision(tool="get_application_answers"),
        decision(action="complete", message="Checked which answers exist."),
    )
    run = start_agent_run(db, "Which answers do I have?", mode="read_only", max_iterations=4)
    _runner(session_factory, exec_settings, exec_profile, browser, claude).execute(run.id)
    db.expire_all()
    finished = db.get(AgentRun, run.id)
    assert finished is not None and finished.status is AgentRunStatus.COMPLETED
    actions = db.scalars(select(AgentAction).where(AgentAction.run_id == run.id)).all()
    tool_outputs = [a.output for a in actions if a.tool == "get_application_answers"]
    assert tool_outputs and "work_authorization" in str(tool_outputs)  # keys are there...
    assert _leaks(exec_profile, {"actions": [a.output for a in actions]}) == []  # values not
    identity = {"first_name", "last_name"} | {
        f"link {k}" for k in exec_profile.verified_profile.links
    }
    assert [k for k in _leaks(exec_profile, {"prompts": claude.prompts}) if k not in identity] == []


# --- B7: daily limits, enforced in the backend ---------------------------------------------


def with_limits(settings: Settings, tmp_path: Path, company_site: int, total: int = 20) -> Settings:
    path = tmp_path / "automation.yaml"
    path.write_text(
        f"daily_limits: {{total: {total}, naukri: 15, linkedin: 5, company_site: {company_site}}}",
        encoding="utf-8",
    )
    return settings.model_copy(update={"automation_path": path})


def test_daily_limit_refuses_to_open_a_live_session(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    tmp_path: Path,
) -> None:
    for settings in (
        with_limits(live_settings, tmp_path, company_site=0),
        with_limits(live_settings, tmp_path, company_site=10, total=0),
    ):
        with pytest.raises(svc.ExecutionError) as exc:
            svc.open_application(ectx(db, settings, exec_profile, browser, live=True), ready_job)
        assert exc.value.code == "DAILY_LIMIT_REACHED"
        assert job_state(db, ready_job) is S.READY_TO_APPLY  # nothing was started
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    refusals = db.scalars(
        select(ExternalActionLog).where(ExternalActionLog.result == ExternalActionResult.REFUSED)
    ).all()
    assert len(refusals) == 2 and all("limit reached" in r.summary for r in refusals)
    assert site.submissions == []


def test_daily_limit_is_checked_again_right_before_submit(
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    tmp_path: Path,
) -> None:
    settings = with_limits(live_settings, tmp_path, company_site=1)
    sid = prepare_live(db, settings, exec_profile, browser, ready_job)
    confirm(db, settings, sid)
    used_up = with_limits(live_settings, tmp_path, company_site=0)  # e.g. another job went first
    agent = tctx(db, used_up, exec_profile, browser, LIVE)
    refused = call(agent, "submit_application", session_id=sid)
    assert refused.error_code is ToolErrorCode.DAILY_LIMIT_REACHED
    assert site.submissions == [] and db.scalar(select(SubmissionAttempt)) is None
    assert job_state(db, ready_job) is S.READY_FOR_SUBMISSION
    call(agent, "close_application_session", session_id=sid)


def test_live_submissions_use_up_the_limit_and_dry_runs_never_do(
    db: Session,
    exec_settings: Settings,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
    site: ApplySite,
    tmp_path: Path,
) -> None:
    from backend.services.limits import usage

    def used() -> dict[str, int]:
        db.expire_all()
        rows = usage(db, settings.automation_path, settings.timezone)
        return {r.platform: r.used for r in rows}

    # A dry run with a used-up limit still runs (it never submits) and uses nothing.
    dry_settings = with_limits(exec_settings, tmp_path, company_site=0)
    out = svc.run_execution(ectx(db, dry_settings, exec_profile, browser), ready_job)
    assert out["dry_run_complete"] is True and out["state"] == "CLOSED", out
    settings = with_limits(live_settings, tmp_path, company_site=1)
    assert used() == {"total": 0, "naukri": 0, "linkedin": 0, "company_site": 0}

    sid = prepare_live(db, settings, exec_profile, browser, ready_job)
    confirm(db, settings, sid)
    agent = tctx(db, settings, exec_profile, browser, LIVE)
    assert call(agent, "submit_application", session_id=sid).ok
    assert used() == {"total": 1, "naukri": 0, "linkedin": 0, "company_site": 1}

    attempt = db.scalar(select(SubmissionAttempt))
    assert attempt is not None
    attempt.outcome = SubmissionOutcome.USER_NOT_SUBMITTED  # the user says it never went out
    db.commit()
    assert used()["company_site"] == 0
    attempt.outcome = SubmissionOutcome.AMBIGUOUS  # may have reached the employer: counts
    db.commit()
    assert used()["company_site"] == 1
    from datetime import timedelta

    attempt.attempted_at = utcnow() - timedelta(days=1)  # yesterday's don't count today
    db.commit()
    assert used()["company_site"] == 0


def test_api_shows_todays_limits(client: Any, api: Any) -> None:
    rows = client.get("/api/dashboard/limits").json()
    assert [r["platform"] for r in rows] == ["total", "naukri", "linkedin", "company_site"]
    assert {r["platform"]: r["limit"] for r in rows} == {
        "total": 20,
        "naukri": 15,
        "linkedin": 5,
        "company_site": 10,
    }
    assert all(r["used"] == 0 and r["remaining"] == r["limit"] for r in rows)
