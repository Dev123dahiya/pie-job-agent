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
    browser._call(lambda: browser._w.live[sid].page.locator("form").evaluate("f => f.submit()"))
    browser._call(lambda: browser._w.live[sid].page.wait_for_timeout(500))
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

    holder = {"settings": exec_settings, "profile": exec_profile, "browser": browser}
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
        # Read-only platform pages (C3/D): the selectors spec comes from a selectors file,
        # never from the agent; no agent tool reaches these.
        "read_cards",
        "read_page_text",
        # Instahyre search: a GET to the platform's own search address, from its logged-in
        # page; the allowed address prefix comes from the selectors file.
        "fetch_json",
        # The one Naukri click: only from naukri_apply.submit after the user's "yes".
        "click_platform_apply",
        # LinkedIn Easy Apply panel: open, read, Next/Review/Back only, Submit (refused for
        # dry runs; only after the user's "yes"), Dismiss + Discard.
        "li_open",
        "panel_snapshot",
        "panel_click",
        "panel_submit",
        "panel_discard",
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
    return browser._call(lambda: len(browser._w.browser.contexts) if browser._w.browser else 0)


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
    assert used() == {"total": 0, "naukri": 0, "linkedin": 0, "company_site": 0, "instahyre": 0}

    sid = prepare_live(db, settings, exec_profile, browser, ready_job)
    confirm(db, settings, sid)
    agent = tctx(db, settings, exec_profile, browser, LIVE)
    assert call(agent, "submit_application", session_id=sid).ok
    assert used() == {"total": 1, "naukri": 0, "linkedin": 0, "company_site": 1, "instahyre": 0}

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
    assert [r["platform"] for r in rows] == [
        "total", "naukri", "linkedin", "company_site", "instahyre",
    ]  # fmt: skip
    assert {r["platform"]: r["limit"] for r in rows} == {
        "total": 40,
        "naukri": 15,
        "linkedin": 5,
        "company_site": 10,
        "instahyre": 5,
    }
    assert all(r["used"] == 0 and r["remaining"] == r["limit"] for r in rows)


# --- D3: Applications page ------------------------------------------------------------------


def test_api_applications_show_resume_lock_status_and_filters(
    client: Any,
    api: Any,
    db: Session,
    live_settings: Settings,
    exec_profile: UserProfile,
    browser: Any,
    ready_job: uuid.UUID,
) -> None:
    app_id = _app_id(db, ready_job)
    rows = client.get("/api/applications").json()
    assert [r["id"] for r in rows] == [app_id]
    row = rows[0]
    assert row["user_status"] == "found" and row["platform"] == "company_site"
    assert row["resume_locked"] is False
    detail = client.get(f"/api/applications/{app_id}").json()
    resume = detail["resume"]
    assert resume["version_number"] >= 1 and resume["locked"] is False
    assert resume["file_name"] and "/" not in resume["file_name"]

    # A prepared live session puts the resume in the employer's form: locked.
    prepare_live(db, live_settings, exec_profile, browser, ready_job)
    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["resume"]["locked"] is True and detail["resume"]["lock_reason"]
    # Filled answers are shown by label and key only, never by value.
    filled = detail["sessions"][0]["filled_fields"]
    assert filled and all(set(f) == {"field_id", "label", "key", "source"} for f in filled)

    def count(**params: Any) -> int:
        res = client.get("/api/applications", params=params)
        assert res.status_code == 200, res.text
        return len(res.json())

    assert count(platform="company_site") == 1 and count(platform="naukri") == 0
    assert count(status_group="found") == 1 and count(status_group="applied") == 0
    assert count(updated_from="2000-01-01") == 1 and count(updated_to="2000-01-01") == 0
    bad = client.get(
        "/api/applications", params={"updated_from": "2026-02-02", "updated_to": "2026-01-01"}
    )
    assert bad.status_code == 422
    assert client.get("/api/applications", params={"platform": "indeed"}).status_code == 422


# --- C8: sessions of a run that stopped are cleaned up -------------------------------------


def test_sessions_of_a_dead_run_are_failed_but_paused_ones_are_kept(
    db: Session, ready_job: uuid.UUID
) -> None:
    from datetime import timedelta

    from backend.services.agent_runner import start_agent_run
    from backend.services.runs import STALE_AFTER, active_run
    from database.enums import AgentRunStatus

    job = db.get(Job, ready_job)
    assert job is not None
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert app is not None
    run = start_agent_run(db, "Apply", mode="read_only", max_iterations=4)
    run.status = AgentRunStatus.RUNNING
    run.heartbeat_at = utcnow() - STALE_AFTER - timedelta(minutes=1)  # the backend died
    job.pipeline_state = S.APPLICATION_IN_PROGRESS

    def session(state: B, run_id: uuid.UUID | None) -> BrowserSession:
        s = BrowserSession(
            application_id=app.id, job_id=job.id, run_id=run_id, mode=BrowserExecutionMode.DRY_RUN,
            state=state, target_url="http://127.0.0.1/x", target_domain="127.0.0.1",
            detected_fields=[], filled_fields=[],
            skipped_fields=[], missing_answers=[], challenges=[], screenshots=[], errors=[],
            blocked_requests=[],
        )  # fmt: skip
        db.add(s)
        return s

    filling = session(B.FILLING, run.id)
    paused = session(B.WAITING_FOR_USER, run.id)  # waits for the user: never touched
    manual = session(B.NAVIGATING, None)  # a user's own dry run: resumable, never touched
    db.commit()

    assert active_run(db) is None
    db.expire_all()
    assert db.get(BrowserSession, filling.id).state is B.FAILED  # type: ignore[union-attr]
    assert db.get(BrowserSession, paused.id).state is B.WAITING_FOR_USER  # type: ignore[union-attr]
    assert db.get(BrowserSession, manual.id).state is B.NAVIGATING  # type: ignore[union-attr]
    assert job_state(db, ready_job) is S.FAILED
    event = db.scalars(
        select(ApplicationEvent).where(ApplicationEvent.event_type == "execution_failed")
    ).one()
    assert event.evidence and event.evidence["stage"] == "run_stopped"
    # The failed job shows up for a manual retry, with the reason.
    from backend.services.failed_jobs import list_failed_jobs

    failed = list_failed_jobs(db)
    assert [f.job_id for f in failed] == [str(ready_job)] and "Nothing was submitted" in (
        failed[0].error or ""
    )
    assert active_run(db) is None  # running it again changes nothing


# --- C5: Needs my input ---------------------------------------------------------------------


def test_needs_input_lists_form_questions_and_answers_unblock_the_job(
    client: Any,
    api: Any,
    db: Session,
    ready_job: uuid.UUID,
    site: ApplySite,
    entered: dict[str, str],
) -> None:
    use_case(db, ready_job, site, "unknown")
    app_id = _app_id(db, ready_job)
    assert client.post(f"/api/applications/{app_id}/dry-run").json()["blocked_needs_user"]

    body = client.get("/api/needs-input").json()
    assert [j["job_id"] for j in body["jobs"]] == [str(ready_job)]
    job = body["jobs"][0]
    assert job["in_browser"] is True and job["application_id"] == app_id
    [q] = [q for q in job["questions"] if q["answerable"]]
    assert q["kind"] == "form" and q["question_key"].startswith("q_")
    assert body["saved_answers"] == []

    res = client.post(
        "/api/needs-input/answers",
        json={
            "answers": [
                {"question_key": q["question_key"], "question": q["question"], "answer": "No"}
            ]
        },
    )
    assert res.status_code == 200 and res.json() == {"saved": 1, "requeued": [str(ready_job)]}
    body = client.get("/api/needs-input").json()
    saved = body["saved_answers"]
    assert [(s["question_key"], s["answer"]) for s in saved] == [(q["question_key"], "No")]
    assert body["jobs"][0]["questions"] == []  # answered: nothing left to ask
    # The event records which question was answered, never the answer.
    event = db.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.event_type == "user_answered_questions")
        .order_by(ApplicationEvent.occurred_at.desc())
    ).first()
    assert event is not None and event.evidence == {"question_keys": [q["question_key"]]}

    # Resuming the dry run uses the saved answer.
    again = client.post(f"/api/applications/{app_id}/dry-run").json()
    assert again["dry_run_complete"] is True and site.submissions == []
    assert "No" in entered.values()

    # Answering again replaces (one saved answer per question); edit and delete work.
    client.post(
        "/api/needs-input/answers",
        json={"answers": [{"question_key": q["question_key"], "answer": "Yes"}]},
    )
    [row] = client.get("/api/needs-input").json()["saved_answers"]
    assert row["answer"] == "Yes"
    edited = client.put(f"/api/needs-input/saved-answers/{row['id']}", json={"answer": "No"})
    assert edited.status_code == 200 and edited.json()["answer"] == "No"
    assert client.delete(f"/api/needs-input/saved-answers/{row['id']}").status_code == 204
    assert client.get("/api/needs-input").json()["saved_answers"] == []
    assert client.delete(f"/api/needs-input/saved-answers/{row['id']}").status_code == 404


def test_needs_input_standard_answers_and_refusals(
    client: Any,
    api: Any,
    db: Session,
    exec_profile: UserProfile,
    ready_job: uuid.UUID,
) -> None:
    from backend.services.readiness import apply_readiness, evaluate_readiness

    # The profile has no current CTC: the job waits for it before any browser opens.
    answers = dict(exec_profile.verified_profile.verified_answers)
    answers.pop("current_ctc", None)
    profile = exec_profile.model_copy(
        update={
            "verified_profile": exec_profile.verified_profile.model_copy(
                update={"verified_answers": answers}
            )
        }
    )
    from backend.api import deps

    client.app.dependency_overrides[deps.profile_dep] = lambda: profile
    job = db.get(Job, ready_job)
    app = db.scalar(select(Application).where(Application.job_id == ready_job))
    assert job is not None and app is not None
    apply_readiness(db, job, app, evaluate_readiness(db, api["settings"], profile, job, app))
    db.commit()
    assert job_state(db, ready_job) is S.BLOCKED_NEEDS_USER

    [paused] = client.get("/api/needs-input").json()["jobs"]
    assert paused["in_browser"] is False
    assert [q["question_key"] for q in paused["questions"]] == ["current_ctc"]

    res = client.post(
        "/api/needs-input/answers",
        json={"answers": [{"question_key": "current_ctc", "answer": "0 (fresher)"}]},
    )
    assert res.json() == {"saved": 1, "requeued": [str(ready_job)]}
    assert job_state(db, ready_job) is S.READY_TO_APPLY  # back in the queue
    assert client.get("/api/needs-input").json()["jobs"] == []

    def post(key: str, answer: str = "x") -> int:
        payload = {"answers": [{"question_key": key, "answer": answer}]}
        return client.post("/api/needs-input/answers", json=payload).status_code

    assert post("otp") == 422  # PIE never stores one-time codes or passwords
    assert post("challenge_captcha") == 422
    assert post("Bad Key!") == 422
    assert post("notice_period", "") == 422
    assert post("notice_period", "x" * 2001) == 422


# --- C9: Failed page ------------------------------------------------------------------------


def test_failed_page_api_screenshot_and_retry_selected(
    client: Any, api: Any, db: Session, failed_job: uuid.UUID
) -> None:
    [item] = client.get("/api/jobs/failed").json()["items"]
    assert item["application_id"] and item["session_id"] and item["screenshot_index"] is not None
    shot = client.get(
        f"/api/applications/{item['application_id']}/sessions/{item['session_id']}"
        f"/screenshots/{item['screenshot_index']}"
    )
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"

    missing = str(uuid.uuid4())
    res = client.post("/api/jobs/retry-batch", json={"job_ids": [str(failed_job), missing]})
    body = res.json()
    assert res.status_code == 200 and body["retried"] == 1
    ok, bad = body["results"]
    assert ok["ok"] and ok["status"] != "FAILED"
    assert not bad["ok"] and "No job" in bad["error"]
    assert client.get("/api/jobs/failed").json()["total"] == 0
    assert client.post("/api/jobs/retry-batch", json={"job_ids": []}).status_code == 422


# --- C9: Runs page (pipeline runs) -----------------------------------------------------------


def _start(client: Any, **body: Any) -> Any:
    return client.post("/api/runs", json=body)


def test_runs_dry_run_apply_never_submits(
    client: Any, api: Any, db: Session, ready_job: uuid.UUID, site: ApplySite
) -> None:
    res = _start(client, steps=["apply"])
    assert res.status_code == 202, res.text
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["status"] == "COMPLETED" and run["mode"] == "dry-run"
    assert run["report"]["counts"] == {"dry_run_complete": 1}
    assert site.submissions == [] and job_state(db, ready_job) is S.READY_TO_APPLY
    assert [r["id"] for r in client.get("/api/runs").json()["items"]] == [run["id"]]
    # Stopping a finished run changes nothing; unknown runs are 404.
    assert client.post(f"/api/runs/{run['id']}/stop").json()["status"] == "COMPLETED"
    assert client.post(f"/api/runs/{uuid.uuid4()}/stop").status_code == 404


def test_runs_refuse_what_is_not_allowed(client: Any, api: Any, ready_job: uuid.UUID) -> None:
    assert _start(client, steps=[]).status_code == 422
    assert _start(client, steps=["fly"]).status_code == 422
    assert _start(client, steps=["apply"], mode="auto").status_code == 422  # not built
    assert _start(client, steps=["analyze"], analysis_limit=51).status_code == 422
    # Review mode is live: refused while DEVELOPMENT_MODE / the live switch say no.
    res = _start(client, steps=["apply"], mode="review")
    assert res.status_code == 422 and "DEVELOPMENT_MODE" in res.json()["detail"]
    opts = client.get("/api/runs/options").json()
    assert opts["default_mode"] == "dry-run" and opts["live_possible"] is False
    assert all(p["apply_supported"] for p in opts["platforms"])  # every platform is built


def test_runs_review_mode_needs_the_exact_confirmation_and_never_submits(
    client: Any,
    api: Any,
    db: Session,
    live_settings: Settings,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    api["settings"] = live_settings
    opts = client.get("/api/runs/options").json()
    assert opts["live_possible"] is True
    company = next(p for p in opts["platforms"] if p["platform"] == "company_site")
    phrase = company["review_confirmation"]["phrase"]
    assert phrase == "LIVE on company_site: up to 10 today"
    assert _start(client, steps=["apply"], mode="review").status_code == 422
    assert _start(client, steps=["apply"], mode="review", confirmation="yes").status_code == 422
    res = _start(client, steps=["apply"], mode="review", confirmation=phrase)
    assert res.status_code == 202, res.text
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["report"]["counts"] == {"waiting_for_review": 1}
    assert job_state(db, ready_job) is S.READY_FOR_SUBMISSION  # waits for the user's yes/no
    assert site.submissions == []
    session = db.scalars(select(BrowserSession).where(BrowserSession.job_id == ready_job)).one()
    assert session.mode is BrowserExecutionMode.LIVE and session.user_confirmed_at is None
    svc.close_session(
        svc.ExecContext(
            db, live_settings, api["profile"], api["browser"], BrowserExecutionMode.LIVE
        ),
        session.id,
    )


def test_runs_tailor_step_makes_resume_and_draft(
    client: Any,
    api: Any,
    db: Session,
    shortlisted_job_id: Any,
) -> None:
    from backend.api.routes.runs import claude_factory_dep

    client.app.dependency_overrides[claude_factory_dep] = lambda: lambda: FakeClaude(good_resume())
    res = _start(client, steps=["tailor"], tailor_limit=1)
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["status"] == "COMPLETED", run
    assert run["report"]["counts"].get("tailored") == 1
    assert job_state(db, shortlisted_job_id) in (S.READY_TO_APPLY, S.BLOCKED_NEEDS_USER)


def test_runs_discover_step_survives_a_failing_source(client: Any, api: Any) -> None:
    res = _start(client, steps=["discover"])
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["status"] == "COMPLETED"  # a source that can't be built is a reported problem


# --- C6: Review queue -------------------------------------------------------------------------


def _review_run(client: Any, api: Any, live_settings: Settings) -> list[dict[str, Any]]:
    api["settings"] = live_settings
    phrase = next(
        p for p in client.get("/api/runs/options").json()["platforms"]
        if p["platform"] == "company_site"
    )["review_confirmation"]["phrase"]  # fmt: skip
    assert _start(client, steps=["apply"], mode="review", confirmation=phrase).status_code == 202
    return list(client.get("/api/review").json()["items"])


def test_review_queue_shows_what_will_be_sent_and_approve_submits_once(
    client: Any,
    api: Any,
    db: Session,
    live_settings: Settings,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    [item] = _review_run(client, api, live_settings)
    assert item["job_id"] == str(ready_job) and item["platform"] == "company_site"
    assert item["resume"] and item["uploaded_resume"].endswith(".pdf")
    keys = {a["key"] for a in item["answers"]}
    assert {"email", "notice_period"} <= keys
    assert all(set(a) == {"label", "key", "source", "ai_suggested"} for a in item["answers"])
    assert not any(a["ai_suggested"] for a in item["answers"])
    assert client.get("/api/dashboard/home").json()["quick_links"]["waiting_for_review"] == 1
    url = f"/api/review/{item['session_id']}/approve"

    wrong = client.post(url, json={"statement": "I agree to whatever"})
    assert wrong.status_code == 422 and site.submissions == []
    ok = client.post(url, json={"statement": item["confirmation_statement"]})
    assert ok.status_code == 200 and ok.json()["submitted"] is True
    assert len(site.submissions) == 1 and job_state(db, ready_job) is S.SUBMITTED
    again = client.post(url, json={"statement": item["confirmation_statement"]})
    assert again.status_code == 409 and len(site.submissions) == 1
    assert client.get("/api/review").json()["total"] == 0


def test_review_reject_submits_nothing_and_withdraws(
    client: Any,
    api: Any,
    db: Session,
    live_settings: Settings,
    ready_job: uuid.UUID,
    site: ApplySite,
) -> None:
    [item] = _review_run(client, api, live_settings)
    res = client.post(f"/api/review/{item['session_id']}/reject")
    assert res.status_code == 200 and res.json()["status"] == "WITHDRAWN"
    assert site.submissions == [] and job_state(db, ready_job) is S.WITHDRAWN
    assert client.post(f"/api/review/{item['session_id']}/reject").status_code == 409
    assert client.post(f"/api/review/{uuid.uuid4()}/reject").status_code == 404


def test_review_batch_approval_keeps_every_check(
    client: Any,
    api: Any,
    db: Session,
    live_settings: Settings,
    ready_job: uuid.UUID,
    site: ApplySite,
    tmp_path: Path,
) -> None:
    [item] = _review_run(client, api, live_settings)
    api["settings"] = with_limits(live_settings, tmp_path, company_site=0)  # limit used up
    res = client.post(
        "/api/review/approve-batch",
        json={"items": [
            {"session_id": item["session_id"], "statement": item["confirmation_statement"]},
            {"session_id": str(uuid.uuid4()), "statement": "x" * 20},
        ]},
    )  # fmt: skip
    body = res.json()
    assert res.status_code == 200 and body["submitted"] == 0
    first, second = body["results"]
    assert first["code"] == "DAILY_LIMIT_REACHED" and site.submissions == []
    assert second["submitted"] is False


# --- C4: Naukri apply, review mode only (fake Naukri browser: no site is contacted) ----------


class FakeNaukri:
    def __init__(
        self,
        button: str = "Apply",
        after: str = "Applied",
        follow_up: bool = False,
        applied: bool | None = None,
    ):
        self.button, self.after, self.follow_up, self.applied = button, after, follow_up, applied
        self.clicks: list[str] = []

    def read_cards(self, url: str, spec: dict[str, Any], *, render_all: bool = False) -> Any:
        ids = "company-site-button" if self.button == "Apply on company site" else "apply-button"
        return {"status": 200, "url": url, "text": "job page",
                "items": [{"id": ids, "text": self.button}]}  # fmt: skip

    def click_platform_apply(
        self, url: str, button: str, expect: str, follow_up: str, applied_text: str = "Applied"
    ) -> Any:
        self.clicks.append(url)
        self.expected = (button, expect, applied_text)
        out = {"url": url, "button_after": self.after, "follow_up": self.follow_up, "text": ""}
        if self.applied is not None:
            out["applied"] = self.applied
        return out


@pytest.fixture
def naukri_job(client: Any, api: Any, db: Session) -> uuid.UUID:
    job = Job(
        source="naukri", source_job_id="011026008731", title="Python Developer",
        url="https://www.naukri.com/job-listings-python-developer-acme-011026008731",
        location="Gurugram", description="Python, FastAPI", posted_at=utcnow(),
        pipeline_state=S.SHORTLISTED,
    )  # fmt: skip
    db.add(job)
    db.commit()
    res = client.post(f"/api/jobs/{job.id}/application-draft", json={})
    assert res.status_code == 200, res.text
    assert job_state(db, job.id) is S.READY_TO_APPLY  # no tailored resume needed on Naukri
    return job.id


def _fake_naukri(client: Any, fake: FakeNaukri) -> None:
    from backend.api.deps import platform_browser_dep

    client.app.dependency_overrides[platform_browser_dep] = lambda: lambda platform: fake


def _naukri_phrase(client: Any) -> str:
    opts = client.get("/api/runs/options").json()
    naukri = next(p for p in opts["platforms"] if p["platform"] == "naukri")
    assert naukri["apply_supported"] is True
    return str(naukri["review_confirmation"]["phrase"])


def test_naukri_dry_run_only_reports(client: Any, api: Any, db: Session, naukri_job: Any) -> None:
    fake = FakeNaukri()
    _fake_naukri(client, fake)
    res = _start(client, steps=["apply"], platforms=["naukri"])
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["report"]["counts"] == {"naukri_would_wait_for_review": 1}
    assert fake.clicks == [] and job_state(db, naukri_job) is S.READY_TO_APPLY


def test_naukri_review_then_approve_clicks_once(
    client: Any, api: Any, db: Session, live_settings: Settings, naukri_job: Any
) -> None:
    from backend.services.limits import usage

    fake = FakeNaukri()
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    phrase = _naukri_phrase(client)
    assert phrase == "LIVE on naukri: up to 15 today"
    res = _start(client, steps=["apply"], platforms=["naukri"], mode="review", confirmation=phrase)
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["report"]["counts"] == {"naukri_waiting_for_review": 1} and fake.clicks == []
    [item] = client.get("/api/review").json()["items"]
    assert item["platform"] == "naukri" and item["answers"] == []
    ok = client.post(
        f"/api/review/{item['session_id']}/approve",
        json={"statement": item["confirmation_statement"]},
    )
    assert ok.status_code == 200 and ok.json()["submitted"] is True, ok.json()
    assert len(fake.clicks) == 1 and job_state(db, naukri_job) is S.SUBMITTED
    used = {
        r.platform: r.used for r in usage(db, live_settings.automation_path, live_settings.timezone)
    }
    assert used["naukri"] == 1 and used["total"] == 1
    again = client.post(
        f"/api/review/{item['session_id']}/approve",
        json={"statement": item["confirmation_statement"]},
    )
    assert again.status_code == 409 and len(fake.clicks) == 1  # never twice


def test_naukri_company_site_jobs_go_to_manual_apply(
    client: Any, api: Any, db: Session, live_settings: Settings, naukri_job: Any
) -> None:
    _fake_naukri(client, FakeNaukri(button="Apply on company site"))
    api["settings"] = live_settings
    _start(client, steps=["apply"], platforms=["naukri"], mode="review",
           confirmation=_naukri_phrase(client))  # fmt: skip
    assert job_state(db, naukri_job) is S.MANUAL_APPLY
    [item] = client.get("/api/jobs/manual").json()["items"]
    assert "company's own site" in item["reason"]


def test_naukri_follow_up_questions_pause_for_the_user(
    client: Any, api: Any, db: Session, live_settings: Settings, naukri_job: Any
) -> None:
    fake = FakeNaukri(after="Apply", follow_up=True)
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    _start(client, steps=["apply"], platforms=["naukri"], mode="review",
           confirmation=_naukri_phrase(client))  # fmt: skip
    [item] = client.get("/api/review").json()["items"]
    out = client.post(
        f"/api/review/{item['session_id']}/approve",
        json={"statement": item["confirmation_statement"]},
    ).json()
    assert out["submitted"] is False and out["outcome"] == "AMBIGUOUS"
    assert job_state(db, naukri_job) is S.BLOCKED_NEEDS_USER and len(fake.clicks) == 1
    app_id = _app_id(db, naukri_job)
    resolved = client.post(
        f"/api/applications/{app_id}/sessions/{item['session_id']}/resolve",
        json={"submitted": True, "note": "Answered Naukri's questions myself"},
    )
    assert resolved.status_code == 200 and job_state(db, naukri_job) is S.SUBMITTED


def test_naukri_applied_button_wins_over_a_chat_element_on_the_page(
    client: Any, api: Any, db: Session, live_settings: Settings, naukri_job: Any
) -> None:
    # Live 2026-10-04: a "chatbot" element was on the page while Naukri showed "Applied".
    fake = FakeNaukri(after="Apply", follow_up=True, applied=True)
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    _start(client, steps=["apply"], platforms=["naukri"], mode="review",
           confirmation=_naukri_phrase(client))  # fmt: skip
    [item] = client.get("/api/review").json()["items"]
    out = client.post(
        f"/api/review/{item['session_id']}/approve",
        json={"statement": item["confirmation_statement"]},
    ).json()
    assert out["submitted"] is True and out["outcome"] == "CONFIRMED"
    assert job_state(db, naukri_job) is S.SUBMITTED and len(fake.clicks) == 1


def test_naukri_approve_respects_the_daily_limit(
    client: Any, api: Any, db: Session, live_settings: Settings, naukri_job: Any, tmp_path: Path
) -> None:
    fake = FakeNaukri()
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    _start(client, steps=["apply"], platforms=["naukri"], mode="review",
           confirmation=_naukri_phrase(client))  # fmt: skip
    [item] = client.get("/api/review").json()["items"]
    path = tmp_path / "automation.yaml"
    path.write_text(
        "daily_limits: {total: 20, naukri: 1, linkedin: 0, company_site: 10}", encoding="utf-8"
    )
    api["settings"] = live_settings.model_copy(update={"automation_path": path})
    other = Job(source="naukri", source_job_id="999", title="Data Engineer",
                url="https://www.naukri.com/job-listings-data-engineer-999",
                pipeline_state=S.SUBMITTED)  # fmt: skip
    db.add(other)
    db.flush()
    other_app = Application(job_id=other.id)
    db.add(other_app)
    db.flush()
    db.add(SubmissionAttempt(idempotency_key="x" * 64, application_id=other_app.id,
                             outcome=SubmissionOutcome.CONFIRMED))  # one sent today  # fmt: skip
    db.commit()
    out = client.post(
        f"/api/review/{item['session_id']}/approve",
        json={"statement": item["confirmation_statement"]},
    ).json()
    assert out["submitted"] is False and out.get("code") in (
        "DAILY_LIMIT_REACHED",
        "DUPLICATE_APPLICATION",
    )
    assert fake.clicks == []


# --- D: LinkedIn Easy Apply against a local, LinkedIn-shaped page --------------------------


@pytest.fixture(scope="module")
def li_site() -> Iterator[Any]:
    from tests.fixtures.linkedin_site import LinkedInSite

    site = LinkedInSite().start()
    yield site
    site.stop()


@pytest.fixture
def li_job(client: Any, api: Any, db: Session, li_site: Any, browser: Any) -> Any:
    from backend.api.deps import platform_browser_dep

    li_site.submissions.clear()
    li_site.discards.clear()
    client.app.dependency_overrides[platform_browser_dep] = lambda: lambda platform: browser

    def make(case: str) -> uuid.UUID:
        job = Job(
            source="linkedin", source_job_id=f"447533{len(case)}{case[:2]}",
            title="Python Developer",
            url=li_site.url(case), location="Gurugram, India", description="Python",
            posted_at=utcnow(), pipeline_state=S.SHORTLISTED,
        )  # fmt: skip
        db.add(job)
        db.commit()
        assert client.post(f"/api/jobs/{job.id}/application-draft", json={}).status_code == 200
        assert job_state(db, job.id) is S.READY_TO_APPLY  # no tailored resume on LinkedIn
        return job.id

    return make


def _li_run(client: Any, **body: Any) -> dict[str, Any]:
    res = _start(client, steps=["apply"], platforms=["linkedin"], **body)
    assert res.status_code == 202, res.text
    return dict(client.get(f"/api/runs/{res.json()['run_id']}").json())


def test_linkedin_dry_run_fills_every_step_and_discards(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any, exec_profile: UserProfile
) -> None:
    job_id = li_job("normal")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_dry_run_complete": 1}, run["report"]
    assert li_site.submissions == [] and li_site.discards == ["normal"]
    assert job_state(db, job_id) is S.READY_TO_APPLY
    session = db.scalars(select(BrowserSession).where(BrowserSession.job_id == job_id)).one()
    keys = {f["key"] for f in session.filled_fields}
    assert {"phone", "work_authorization", "notice_period"} <= keys
    assert _leaks(exec_profile, {"session": session.filled_fields}) == []  # labels and keys only


def test_linkedin_unknown_question_pauses_for_the_user(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    job_id = li_job("unknown")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_needs_input": 1}
    assert job_state(db, job_id) is S.BLOCKED_NEEDS_USER and li_site.submissions == []
    [paused] = client.get("/api/needs-input").json()["jobs"]
    assert any("Kubernetes" in q["question"] for q in paused["questions"])


def test_linkedin_stuck_form_fails_cleanly(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    job_id = li_job("stuck")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_failed": 1}
    assert job_state(db, job_id) is S.FAILED and li_site.submissions == []


def test_linkedin_review_then_approve_submits_once(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any, live_settings: Settings
) -> None:
    job_id = li_job("normal")
    api["settings"] = live_settings
    phrase = next(
        p
        for p in client.get("/api/runs/options").json()["platforms"]
        if p["platform"] == "linkedin"
    )["review_confirmation"]["phrase"]
    assert phrase == "LIVE on linkedin: up to 5 today"
    run = _li_run(client, mode="review", confirmation=phrase)
    assert run["report"]["counts"] == {"linkedin_waiting_for_review": 1}
    assert li_site.submissions == [] and job_state(db, job_id) is S.READY_FOR_SUBMISSION
    [item] = client.get("/api/review").json()["items"]
    assert item["platform"] == "linkedin"
    url = f"/api/review/{item['session_id']}/approve"
    ok = client.post(url, json={"statement": item["confirmation_statement"]})
    assert ok.json()["submitted"] is True, ok.json()
    assert li_site.submissions == ["normal"] and job_state(db, job_id) is S.SUBMITTED
    assert client.post(url, json={"statement": item["confirmation_statement"]}).status_code == 409
    assert li_site.submissions == ["normal"]


def test_linkedin_dry_run_sessions_can_never_submit(browser: Any, li_site: Any) -> None:
    li_site.submissions.clear()
    sid = str(uuid.uuid4())
    panel = browser.li_open(sid, li_site.url("normal"), dry_run=True,
                            button_name="Easy Apply to this job")  # fmt: skip
    assert panel["heading"] == "Apply to Acme" and "Next" in panel["buttons"]
    with pytest.raises(BrowserError):
        browser.panel_submit(sid)
    with pytest.raises(BrowserError):
        browser.panel_click(sid, "Submit application")  # only Next / Review / Back
    browser.panel_discard(sid, "Dismiss", "Discard")
    assert li_site.submissions == []


# --- C7: a daily-quota / warning message stops the platform at once ------------------------


def test_linkedin_quota_message_stops_and_keeps_the_job(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    first, second = li_job("quota"), li_job("normal")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_quota": 1}  # the second job was not tried
    assert job_state(db, first) is S.READY_TO_APPLY and job_state(db, second) is S.READY_TO_APPLY
    assert li_site.submissions == [] and "quota" in li_site.discards


def test_naukri_quota_message_stops_the_run(
    client: Any, api: Any, db: Session, live_settings: Settings, naukri_job: Any
) -> None:
    fake = FakeNaukri()
    fake.read_cards = lambda url, spec, render_all=False: {  # type: ignore[method-assign]
        "status": 200, "url": url, "items": [{"id": "apply-button", "text": "Apply"}],
        "text": "You have reached your daily apply limit. Try again tomorrow.",
    }  # fmt: skip
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    res = _start(client, steps=["apply"], platforms=["naukri"], mode="review",
                 confirmation=_naukri_phrase(client))  # fmt: skip
    run = client.get(f"/api/runs/{res.json()['run_id']}").json()
    assert run["report"]["counts"] == {"naukri_quota": 1}
    assert job_state(db, naukri_job) is S.READY_TO_APPLY and fake.clicks == []


def test_quota_patterns() -> None:
    from integrations.platforms.base import quota_warning
    from integrations.platforms.linkedin import selectors as li
    from integrations.platforms.naukri import selectors as nk

    assert quota_warning(li, "You've reached the Easy Apply application limit for today")
    assert quota_warning(nk, "You have reached your daily apply limit")
    assert quota_warning(li, "We noticed unusual activity on your account")
    assert quota_warning(nk, "Python developer, 2 years, no limit on growth") is None


def _as_linkedin_job(db: Session, job_id: uuid.UUID, li_site: Any) -> None:
    job = db.get(Job, job_id)
    assert job is not None
    job.source, job.url, job.application_url = "linkedin", li_site.url("normal"), None
    db.commit()


def test_linkedin_dry_run_never_uploads_the_tailored_resume(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any, ready_job: uuid.UUID
) -> None:
    _as_linkedin_job(db, ready_job, li_site)  # has a validated tailored resume
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_dry_run_complete": 1}, run["report"]
    session = db.scalars(select(BrowserSession).where(BrowserSession.job_id == ready_job)).one()
    assert session.uploaded_resume and session.uploaded_resume["status"] == "NOT_UPLOADED_DRY_RUN"


def test_linkedin_review_uploads_the_tailored_resume(
    client: Any,
    api: Any,
    db: Session,
    li_job: Any,
    li_site: Any,
    ready_job: uuid.UUID,
    live_settings: Settings,
) -> None:
    _as_linkedin_job(db, ready_job, li_site)
    api["settings"] = live_settings
    phrase = next(
        p
        for p in client.get("/api/runs/options").json()["platforms"]
        if p["platform"] == "linkedin"
    )["review_confirmation"]["phrase"]
    run = _li_run(client, mode="review", confirmation=phrase)
    assert run["report"]["counts"] == {"linkedin_waiting_for_review": 1}, run["report"]
    session = db.scalars(select(BrowserSession).where(BrowserSession.job_id == ready_job)).one()
    assert session.uploaded_resume and session.uploaded_resume["status"] == "UPLOAD_SUCCESS"
    assert session.uploaded_resume["file_name"].endswith(".pdf")
    [item] = client.get("/api/review").json()["items"]
    assert item["uploaded_resume"].endswith(".pdf")
    ok = client.post(
        f"/api/review/{item['session_id']}/approve",
        json={"statement": item["confirmation_statement"]},
    )
    assert ok.json()["submitted"] is True and li_site.submissions[-1] == "normal"


def test_linkedin_step_buttons_with_long_labels_are_found(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    job_id = li_job("aria")  # "Next" carries aria-label "Continue to next step"
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_dry_run_complete": 1}, run["report"]
    assert job_state(db, job_id) is S.READY_TO_APPLY and li_site.submissions == []


# --- E3: auto mode (submit without a per-application "yes"), gated per platform -----------


def _phrase(client: Any, platform: str, kind: str) -> str:
    opts = client.get("/api/runs/options").json()
    return str(next(p for p in opts["platforms"] if p["platform"] == platform)[kind]["phrase"])


def test_auto_mode_unlocks_after_a_dry_run_and_an_approved_review(
    client: Any,
    api: Any,
    db: Session,
    li_job: Any,
    li_site: Any,
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    first, second = li_job("normal"), li_job("aria")
    api["settings"] = live_settings
    status = lambda: next(  # noqa: E731
        p
        for p in client.get("/api/runs/options").json()["platforms"]
        if p["platform"] == "linkedin"
    )["auto"]
    assert status()["unlocked"] is False and len(status()["missing"]) == 2
    refused = _start(client, steps=["apply"], platforms=["linkedin"], mode="auto",
                     confirmation=_phrase(client, "linkedin", "auto_confirmation"))  # fmt: skip
    assert refused.status_code == 422 and "unlocks after" in refused.json()["detail"]

    # 1. a dry run (both jobs), 2. one application approved by the user in review mode
    api["settings"] = live_settings.model_copy(update={"development_mode": True})
    _li_run(client)
    api["settings"] = live_settings
    assert status()["missing"] == ["one application you approved in review mode"]
    _li_run(client, mode="review", confirmation=_phrase(client, "linkedin", "review_confirmation"))
    [item] = [i for i in client.get("/api/review").json()["items"] if i["job_id"] == str(first)]
    client.post(f"/api/review/{item['session_id']}/approve",
                json={"statement": item["confirmation_statement"]})  # fmt: skip
    # the second job also waits in the review queue from that run: reject it to requeue
    for other in client.get("/api/review").json()["items"]:
        client.post(f"/api/review/{other['session_id']}/reject")
    db.query(Job).filter(Job.id == second).update({"pipeline_state": S.READY_TO_APPLY})
    db.commit()
    assert status()["unlocked"] is True

    run = _li_run(
        client, mode="auto", confirmation=_phrase(client, "linkedin", "auto_confirmation")
    )
    assert run["report"]["counts"].get("auto_submitted") == 1, run["report"]
    assert job_state(db, second) is S.SUBMITTED  # no "yes" from the user for this one
    assert li_site.submissions.count("aria") == 1
    session = db.scalars(
        select(BrowserSession).where(
            BrowserSession.job_id == second, BrowserSession.state == B.SUBMITTED
        )
    ).one()
    assert (session.user_confirmation or "").startswith("AUTO MODE")


def test_auto_mode_still_obeys_the_daily_limit(
    client: Any,
    api: Any,
    db: Session,
    li_job: Any,
    li_site: Any,
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    from backend.services import auto_mode

    li_job("normal")
    api["settings"] = with_limits(live_settings, tmp_path, 10, 0)  # a temporary file, never config/
    original = auto_mode.auto_status
    auto_mode.auto_status = lambda db, p: {"platform": p, "unlocked": True, "missing": []}  # type: ignore[assignment]
    try:
        res = _start(client, steps=["apply"], platforms=["linkedin"], mode="auto",
                     confirmation="AUTO on linkedin: up to 0 today")  # fmt: skip
    finally:
        auto_mode.auto_status = original  # type: ignore[assignment]
    assert res.status_code == 422 and "No live applications left today" in res.json()["detail"]
    assert li_site.submissions == []


def test_linkedin_waits_for_a_slowly_loading_form(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    job_id = li_job("slow")  # the title shows first, the form 2.5 s later
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_dry_run_complete": 1}, run["report"]
    assert job_state(db, job_id) is S.READY_TO_APPLY and li_site.submissions == []


def _shortlisted_naukri_job(db: Session) -> uuid.UUID:
    job = Job(
        source="naukri", source_job_id="041026000001", title="Python Developer",
        url="https://www.naukri.com/job-listings-python-developer-acme-041026000001",
        location="Gurugram", description="Python, FastAPI", posted_at=utcnow(),
        pipeline_state=S.SHORTLISTED,
    )  # fmt: skip
    db.add(job)
    db.commit()
    return job.id


def _daily_auto(session_factory: Any, settings: Settings, profile: UserProfile, fake: Any) -> Any:
    from backend.services.daily_auto import run_daily_auto

    return run_daily_auto(
        session_factory, settings, profile, build_default_registry(), lambda platform: fake
    )


def test_daily_auto_apply_is_off_unless_turned_on(
    db: Session, session_factory: Any, live_settings: Settings, exec_profile: UserProfile
) -> None:
    job_id = _shortlisted_naukri_job(db)
    fake = FakeNaukri()
    out = _daily_auto(session_factory, live_settings, exec_profile, fake)
    assert out == {"skipped": "DAILY_AUTO_APPLY is off"} and fake.clicks == []
    on_dev = live_settings.model_copy(update={"daily_auto_apply": True, "development_mode": True})
    assert "skipped" in _daily_auto(session_factory, on_dev, exec_profile, fake)
    assert fake.clicks == [] and job_state(db, job_id) is S.SHORTLISTED


def test_daily_auto_apply_only_where_auto_is_unlocked(
    db: Session,
    session_factory: Any,
    live_settings: Settings,
    exec_profile: UserProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import daily_auto

    job_id = _shortlisted_naukri_job(db)
    settings = live_settings.model_copy(update={"daily_auto_apply": True})
    fake = FakeNaukri()
    out = _daily_auto(session_factory, settings, exec_profile, fake)
    assert out["naukri"]["skipped"] == "auto mode not unlocked" and fake.clicks == []

    def naukri_only(db: Any, p: str) -> dict[str, Any]:
        return {"platform": p, "unlocked": p == "naukri", "missing": []}

    monkeypatch.setattr(daily_auto, "auto_status", naukri_only)
    monkeypatch.setattr("backend.services.auto_mode.auto_status", naukri_only)
    out = _daily_auto(session_factory, settings, exec_profile, fake)
    assert out["linkedin"]["skipped"] == "auto mode not unlocked"
    assert out["naukri"]["counts"].get("auto_submitted") == 1, out
    db.expire_all()
    assert len(fake.clicks) == 1 and job_state(db, job_id) is S.SUBMITTED
    # Next day's run (same day here): nothing is applied twice.
    again = _daily_auto(session_factory, settings, exec_profile, fake)
    assert again["naukri"]["skipped"] == "nothing ready" and len(fake.clicks) == 1


# --- Instahyre: one-click "Apply now" (shares naukri_apply; fake browser, no site contacted) --


class FakeInstahyreJobPage(FakeNaukri):
    """Instahyre's job page: one ".apply-button button" without an id."""

    def read_cards(self, url: str, spec: dict[str, Any], *, render_all: bool = False) -> Any:
        assert spec["card"] == ".apply-button button"
        return {"status": 200, "url": url, "text": "job page",
                "items": [{"id": None, "text": self.button}]}  # fmt: skip


@pytest.fixture
def instahyre_job(client: Any, api: Any, db: Session) -> uuid.UUID:
    job = Job(
        source="instahyre", source_job_id="433301", title="AI Engineer",
        url="https://www.instahyre.com/job-433301-ai-engineer-at-acme-gurgaon/",
        location="Gurgaon", description="Python, LLM", posted_at=utcnow(),
        pipeline_state=S.SHORTLISTED,
    )  # fmt: skip
    db.add(job)
    db.commit()
    res = client.post(f"/api/jobs/{job.id}/application-draft", json={})
    assert res.status_code == 200, res.text
    assert job_state(db, job.id) is S.READY_TO_APPLY  # no tailored resume on Instahyre
    return job.id


def test_instahyre_dry_run_review_and_one_click(
    client: Any, api: Any, db: Session, live_settings: Settings, instahyre_job: Any
) -> None:
    from backend.services.auto_mode import auto_status

    fake = FakeInstahyreJobPage(button="Apply now", after="Application sent!", applied=True)
    _fake_naukri(client, fake)
    dry = _start(client, steps=["apply"], platforms=["instahyre"])
    run = client.get(f"/api/runs/{dry.json()['run_id']}").json()
    assert run["report"]["counts"] == {"instahyre_would_wait_for_review": 1} and fake.clicks == []
    assert auto_status(db, "instahyre")["missing"] == [
        "one application you approved in review mode"
    ]

    api["settings"] = live_settings
    opts = client.get("/api/runs/options").json()
    ih = next(p for p in opts["platforms"] if p["platform"] == "instahyre")
    phrase = ih["review_confirmation"]["phrase"]
    assert phrase == "LIVE on instahyre: up to 5 today"
    _start(client, steps=["apply"], platforms=["instahyre"], mode="review", confirmation=phrase)
    [item] = client.get("/api/review").json()["items"]
    assert fake.clicks == []
    ok = client.post(f"/api/review/{item['session_id']}/approve",
                     json={"statement": item["confirmation_statement"]})  # fmt: skip
    assert ok.status_code == 200 and ok.json()["submitted"] is True, ok.json()
    assert fake.clicks == [str(db.get(Job, instahyre_job).url)]
    assert fake.expected == (".apply-button button", "Apply now", "Application sent!")
    assert job_state(db, instahyre_job) is S.SUBMITTED
    assert auto_status(db, "instahyre")["unlocked"] is True


def test_instahyre_job_already_applied_is_withdrawn_not_clicked(
    client: Any, api: Any, db: Session, live_settings: Settings, instahyre_job: Any
) -> None:
    fake = FakeInstahyreJobPage(button="Application sent!")
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    opts = client.get("/api/runs/options").json()
    ih = next(p for p in opts["platforms"] if p["platform"] == "instahyre")
    _start(client, steps=["apply"], platforms=["instahyre"], mode="review",
           confirmation=ih["review_confirmation"]["phrase"])  # fmt: skip
    assert job_state(db, instahyre_job) is S.WITHDRAWN and fake.clicks == []


def test_a_covered_apply_button_is_not_clicked_and_nothing_counts(
    client: Any, api: Any, db: Session, live_settings: Settings, instahyre_job: Any
) -> None:
    from backend.services.limits import usage

    class Covered(FakeInstahyreJobPage):
        def click_platform_apply(self, *args: Any, **kw: Any) -> Any:
            raise BrowserError("NOT_CLICKED", "Something covers the apply button (DIV.loader)")

    fake = Covered(button="Apply now")
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    opts = client.get("/api/runs/options").json()
    ih = next(p for p in opts["platforms"] if p["platform"] == "instahyre")
    _start(client, steps=["apply"], platforms=["instahyre"], mode="review",
           confirmation=ih["review_confirmation"]["phrase"])  # fmt: skip
    [item] = client.get("/api/review").json()["items"]
    out = client.post(f"/api/review/{item['session_id']}/approve",
                      json={"statement": item["confirmation_statement"]}).json()  # fmt: skip
    assert out["submitted"] is False and out["outcome"] == "NOT_CLICKED", out
    assert job_state(db, instahyre_job) is S.READY_TO_APPLY  # back in the queue, retryable
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0
    used = {
        r.platform: r.used for r in usage(db, live_settings.automation_path, live_settings.timezone)
    }
    assert used["instahyre"] == 0 and used["total"] == 0


def test_after_a_confirmed_not_sent_the_same_job_can_be_applied_again_once(
    client: Any, api: Any, db: Session, live_settings: Settings, instahyre_job: Any
) -> None:
    fake = FakeInstahyreJobPage(button="Apply now", after="Apply now", applied=False)
    _fake_naukri(client, fake)
    api["settings"] = live_settings
    phrase = next(p for p in client.get("/api/runs/options").json()["platforms"]
                  if p["platform"] == "instahyre")["review_confirmation"]["phrase"]  # fmt: skip

    def review_and_approve() -> Any:
        _start(client, steps=["apply"], platforms=["instahyre"], mode="review", confirmation=phrase)
        [item] = client.get("/api/review").json()["items"]
        statement = {"statement": item["confirmation_statement"]}
        return item, client.post(f"/api/review/{item['session_id']}/approve", json=statement).json()

    item, first = review_and_approve()
    assert first["outcome"] == "AMBIGUOUS" and len(fake.clicks) == 1  # unclear: never re-clicked
    app_id = _app_id(db, instahyre_job)
    client.post(f"/api/applications/{app_id}/sessions/{item['session_id']}/resolve",
                json={"submitted": False, "note": "Instahyre still shows Apply now"})  # fmt: skip
    assert job_state(db, instahyre_job) is S.READY_TO_APPLY
    fake.after, fake.applied = "Application sent!", True
    _, second = review_and_approve()
    assert second["submitted"] is True and len(fake.clicks) == 2


def _one_click_page(cover: bool) -> str:
    from urllib.parse import quote

    overlay = (
        '<div style="position:fixed;inset:0;z-index:9;background:rgba(0,0,0,.2)"></div>'
        if cover
        else ""
    )
    html = (
        "<html><body><div class='apply-button'><a><button class='btn' "
        "onclick=\"window.clicks=(window.clicks||0)+1;this.textContent='Application sent!'\">"
        f"Apply now</button></a></div>{overlay}</body></html>"
    )
    return "data:text/html," + quote(html)


def test_one_click_apply_clicks_once_and_sees_the_applied_button(browser: BrowserManager) -> None:
    out = browser.click_platform_apply(
        _one_click_page(cover=False), ".apply-button button", "Apply now", ".modal.in",
        "Application sent!",
    )  # fmt: skip
    assert out["applied"] is True and out["button_after"] == "Application sent!"


def test_a_button_that_stays_covered_is_never_clicked(browser: BrowserManager) -> None:
    with pytest.raises(BrowserError) as err:
        browser.click_platform_apply(
            _one_click_page(cover=True), ".apply-button button", "Apply now", ".modal.in",
            "Application sent!",
        )  # fmt: skip
    assert err.value.kind == "NOT_CLICKED"


def test_a_lost_linkedin_login_keeps_the_jobs_queued_and_stops(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from backend.core import config as core_config
    from integrations.platforms.base import load_status

    monkeypatch.setattr(core_config, "REPO_ROOT", tmp_path)  # the login status file goes here
    first, second = li_job("loggedout"), li_job("normal")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_login_needed": 1}, run["report"]
    assert job_state(db, first) is S.READY_TO_APPLY  # not failed: retried after the login
    assert job_state(db, second) is S.READY_TO_APPLY  # LinkedIn stopped: not even opened
    assert load_status(tmp_path / "data")["linkedin"]["state"] == "expired"
    assert li_site.submissions == []


def test_linkedin_hidden_radio_questions_are_asked_then_answered(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    job_id = li_job("degree")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_needs_input": 1}, run["report"]["failures"]
    paused = client.get("/api/needs-input").json()["jobs"]
    [question] = [q for j in paused for q in j["questions"] if "Bachelor" in q["question"]]
    assert question["options"] == ["Yes", "No"]
    saved = client.post("/api/needs-input/answers", json={"answers": [
        {"question_key": question["question_key"], "question": question["question"],
         "answer": "Yes"}]})  # fmt: skip
    assert saved.status_code == 200, saved.text
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_dry_run_complete": 1}, run["report"]
    session = db.scalars(
        select(BrowserSession)
        .where(BrowserSession.job_id == job_id)
        .order_by(BrowserSession.created_at.desc())
    ).first()
    assert session is not None
    assert any(f.get("key") == question["question_key"] for f in session.filled_fields or [])
    assert li_site.submissions == []


def test_linkedin_question_drawn_late_is_still_asked(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    job_id = li_job("lateq")
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_needs_input": 1}, run["report"]["failures"]
    assert job_state(db, job_id) is S.BLOCKED_NEEDS_USER and li_site.submissions == []


def test_linkedin_new_confirmation_wording_counts_as_sent(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any, live_settings: Settings
) -> None:
    job_id = li_job("submitted")  # LinkedIn now says "Application submitted" (2026-10-05)
    api["settings"] = live_settings
    phrase = next(p for p in client.get("/api/runs/options").json()["platforms"]
                  if p["platform"] == "linkedin")["review_confirmation"]["phrase"]  # fmt: skip
    _li_run(client, mode="review", confirmation=phrase)
    [item] = client.get("/api/review").json()["items"]
    ok = client.post(f"/api/review/{item['session_id']}/approve",
                     json={"statement": item["confirmation_statement"]})  # fmt: skip
    assert ok.json()["submitted"] is True, ok.json()
    assert li_site.submissions == ["submitted"] and job_state(db, job_id) is S.SUBMITTED


def test_a_live_linkedin_form_paused_for_answers_is_discarded(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any, live_settings: Settings
) -> None:
    job_id = li_job("unknown")  # asks a question PIE has no answer for
    api["settings"] = live_settings
    phrase = next(p for p in client.get("/api/runs/options").json()["platforms"]
                  if p["platform"] == "linkedin")["review_confirmation"]["phrase"]  # fmt: skip
    run = _li_run(client, mode="review", confirmation=phrase)
    assert run["report"]["counts"] == {"linkedin_needs_input": 1}
    assert li_site.discards == ["unknown"] and li_site.submissions == []  # nothing left open
    assert job_state(db, job_id) is S.BLOCKED_NEEDS_USER


def test_the_apply_command_needs_no_daily_switch_and_can_pick_a_platform(
    db: Session,
    session_factory: Any,
    live_settings: Settings,
    exec_profile: UserProfile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import daily_auto
    from backend.services.daily_auto import run_daily_auto

    def unlocked(db: Any, p: str) -> dict[str, Any]:
        return {"platform": p, "unlocked": True, "missing": []}

    monkeypatch.setattr(daily_auto, "auto_status", unlocked)
    monkeypatch.setattr("backend.services.auto_mode.auto_status", unlocked)
    job_id = _shortlisted_naukri_job(db)
    fake = FakeNaukri()
    assert live_settings.daily_auto_apply is False  # the daily switch is off
    out = run_daily_auto(session_factory, live_settings, exec_profile, build_default_registry(),
                         lambda platform: fake, platforms=("naukri",),
                         standing_confirmation=False)  # fmt: skip
    assert list(out) == ["naukri"] and out["naukri"]["counts"].get("auto_submitted") == 1
    db.expire_all()
    assert job_state(db, job_id) is S.SUBMITTED and len(fake.clicks) == 1
    # the live switches still apply: development mode refuses
    dev = live_settings.model_copy(update={"development_mode": True})
    refused = run_daily_auto(session_factory, dev, exec_profile, build_default_registry(),
                             lambda platform: fake, standing_confirmation=False)  # fmt: skip
    assert "skipped" in refused and len(fake.clicks) == 1


@pytest.mark.parametrize("profile_folder", [False, True])
def test_a_browser_that_closed_is_started_again(tmp_path: Path, profile_folder: bool) -> None:
    from urllib.parse import quote

    folder = tmp_path / "profile" if profile_folder else None
    manager = BrowserManager(headless=True, timeout_ms=15_000, user_data_dir=folder)
    page = "data:text/html," + quote("<html><body><p>job page</p></body></html>")
    try:
        assert "job page" in manager.read_page_text(page)["text"]
        # the browser goes away (window closed / last tab closed / crash)
        manager._call(lambda: (manager._w.persistent or manager._w.browser).close())
        assert "job page" in manager.read_page_text(page)["text"]  # reopened, not "closed"
    finally:
        manager.shutdown()


def test_linkedin_option_click_ignored_once_is_pressed_again(
    client: Any, api: Any, db: Session, li_job: Any, li_site: Any
) -> None:
    li_job("lateclick")  # the page ignores the first click on an option (not ready yet)
    assert _li_run(client)["report"]["counts"] == {"linkedin_needs_input": 1}
    [question] = [q for j in client.get("/api/needs-input").json()["jobs"]
                  for q in j["questions"] if "Bachelor" in q["question"]]  # fmt: skip
    client.post("/api/needs-input/answers", json={"answers": [
        {"question_key": question["question_key"], "question": question["question"],
         "answer": "Yes"}]})  # fmt: skip
    run = _li_run(client)
    assert run["report"]["counts"] == {"linkedin_dry_run_complete": 1}, run["report"]["failures"]
    assert li_site.submissions == []
