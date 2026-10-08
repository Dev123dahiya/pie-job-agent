"""Needs my input (REQUIREMENTS 8, 19.2.5): jobs paused because only the user can answer.

Each paused job lists the exact questions it could not answer: questions on the employer's
form (from the browser session) and standard questions missing from the profile. The user
answers each question once; the answer is saved as a reusable answer (application_id NULL,
status VERIFIED, source: the user) and every job waiting on it goes back in the queue:
- paused before the browser (missing standard answers): readiness is re-checked and the job
  becomes READY_TO_APPLY when nothing else is missing;
- paused in the browser (a form question): the session resumes with the new answer next time
  it is opened (the form is planned again with the saved answers).

CAPTCHA, OTP, 2FA and login walls are not questions: they are shown as actions to take in
the browser, never answered or stored. Answer values are only ever returned to the dashboard
on this computer; events and logs record question keys, never values.
"""

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.execution.mapping import CHALLENGE
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.readiness import (
    STANDARD_QUESTIONS,
    apply_readiness,
    evaluate_readiness,
)
from database.base import utcnow
from database.enums import AnswerStatus, BrowserSessionState, EventSource
from database.enums import JobPipelineState as S
from database.models import Application, ApplicationAnswer, ApplicationEvent, BrowserSession, Job

KEY = re.compile(r"^[a-z0-9_]{1,200}$")
USER_SOURCE = "answered by the user in the dashboard on {date}"


class NeedsInputError(ValueError):
    """Invalid input; the message says why."""


@dataclass
class Question:
    question_key: str
    question: str
    kind: str  # standard | form | sensitive | challenge
    reason: str | None = None
    answerable: bool = True  # False for CAPTCHA / OTP / login: act in the browser instead
    options: list[str] = field(default_factory=list)  # a dropdown's own choices, if any


@dataclass
class PausedJob:
    job_id: str
    application_id: str | None
    title: str
    company: str | None
    url: str
    reason: str | None
    paused_at: datetime | None
    in_browser: bool  # paused in a browser session (resume it after answering)
    questions: list[Question] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["paused_at"] = self.paused_at.isoformat() if self.paused_at else None
        return data


def saved_global_answers(db: Session) -> list[ApplicationAnswer]:
    return list(
        db.scalars(
            select(ApplicationAnswer)
            .where(ApplicationAnswer.application_id.is_(None))
            .order_by(ApplicationAnswer.question_key)
        ).all()
    )


def _waiting_session(db: Session, app: Application | None) -> BrowserSession | None:
    if app is None:
        return None
    return db.scalars(
        select(BrowserSession)
        .where(
            BrowserSession.application_id == app.id,
            BrowserSession.state == BrowserSessionState.WAITING_FOR_USER,
        )
        .order_by(BrowserSession.created_at.desc())
    ).first()


def paused_jobs(db: Session, settings: Settings, profile: UserProfile) -> list[PausedJob]:
    """Every BLOCKED_NEEDS_USER job with the exact questions it is waiting for."""
    answered = {a.question_key for a in saved_global_answers(db) if a.answer}
    jobs = db.scalars(
        select(Job).where(Job.pipeline_state == S.BLOCKED_NEEDS_USER).order_by(Job.updated_at)
    ).all()
    out = []
    for job in jobs:
        app = db.scalar(select(Application).where(Application.job_id == job.id))
        session = _waiting_session(db, app)
        questions: list[Question] = []
        seen: set[str] = set()
        if session is not None:
            for m in session.missing_answers or []:
                key = m.get("question_key") or ""
                if not key or key in seen or key in answered:
                    continue
                seen.add(key)
                category = str(m.get("category") or "")
                challenge = category == CHALLENGE
                questions.append(
                    Question(
                        question_key=key,
                        question=str(m.get("question") or key),
                        kind="challenge"
                        if challenge
                        else ("sensitive" if category == "SENSITIVE" else "form"),
                        reason=m.get("reason"),
                        answerable=not challenge,
                        options=[str(o) for o in m.get("options") or [] if str(o).strip()],
                    )
                )
            for c in session.challenges or []:
                kind = str(c.get("type") or "CHALLENGE")
                questions.append(
                    Question(
                        question_key=f"challenge_{kind.lower()}",
                        question=str(c.get("detail") or kind),
                        kind="challenge",
                        reason=str(c.get("user_action") or "Act in the browser, then resume."),
                        answerable=False,
                    )
                )
        readiness = evaluate_readiness(db, settings, profile, job, app)
        for key, text in STANDARD_QUESTIONS.items():
            if key in seen or not any(f"'{key}'" in b for b in readiness.user_blockers):
                continue
            seen.add(key)
            questions.append(Question(question_key=key, question=text, kind="standard"))
        out.append(
            PausedJob(
                job_id=str(job.id),
                application_id=str(app.id) if app else None,
                title=job.title,
                company=job.company.name if job.company else None,
                url=job.application_url or job.url,
                reason=(app.user_action_required if app else None) or job.last_error,
                paused_at=job.updated_at,
                in_browser=session is not None,
                questions=questions,
            )
        )
    return out


def _check(key: str, answer: str) -> tuple[str, str]:
    key = (key or "").strip()
    text = (answer or "").strip()
    if not KEY.match(key):
        raise NeedsInputError(f"Invalid question key {key!r}")
    if key.startswith("challenge_") or key in ("password", "otp"):
        raise NeedsInputError("CAPTCHA, OTP and logins are never answered or stored by PIE")
    if not text:
        raise NeedsInputError(f"The answer to {key!r} is empty")
    if len(text) > 2000:
        raise NeedsInputError(f"The answer to {key!r} is longer than 2000 characters")
    return key, text


def save_answers(
    db: Session, settings: Settings, profile: UserProfile, answers: list[dict[str, str]]
) -> dict[str, Any]:
    """Save reusable answers (one per question key; a new answer replaces the old one) and
    put every job that was only waiting for answers back in the queue."""
    checked = [(*_check(a.get("question_key", ""), a.get("answer", "")), a) for a in answers]
    if not checked:
        raise NeedsInputError("No answers given")
    existing = {a.question_key: a for a in saved_global_answers(db)}
    source = USER_SOURCE.format(date=utcnow().date().isoformat())
    for key, text, item in checked:
        row = existing.get(key)
        if row is None:
            row = ApplicationAnswer(application_id=None, question_key=key)
            db.add(row)
        row.question = (item.get("question") or row.question or key)[:2000]
        row.answer = text
        row.status = AnswerStatus.VERIFIED
        row.source = source
    db.flush()
    keys = [k for k, _, _ in checked]
    requeued = requeue_ready_jobs(db, settings, profile, keys)
    db.commit()
    return {"saved": len(checked), "requeued": requeued}


# Platforms whose application form is opened again from the start after a pause (PIE does
# not keep it open while waiting for the user's answers).
RESTART_DOMAINS = frozenset({"linkedin.com"})


def requeue_ready_jobs(
    db: Session, settings: Settings, profile: UserProfile, keys: list[str]
) -> list[str]:
    """Jobs paused only for answers that now exist: before the browser, they become ready
    again; in the browser, they are marked to resume (the session keeps its place)."""
    requeued = []
    for job in db.scalars(select(Job).where(Job.pipeline_state == S.BLOCKED_NEEDS_USER)).all():
        app = db.scalar(select(Application).where(Application.job_id == job.id))
        if app is None:
            continue
        session = _waiting_session(db, app)
        if session is not None:
            if session.challenges:
                continue  # the user still has to act in the browser
            missing = {m.get("question_key") for m in session.missing_answers or []}
            saved = {a.question_key for a in saved_global_answers(db)}
            if missing and missing <= saved:
                if session.target_domain in RESTART_DOMAINS:
                    # A LinkedIn form is not kept open while it waits: the next run opens it
                    # again from the start and fills the saved answers.
                    session.state, session.closed_at = BrowserSessionState.CLOSED, utcnow()
                    session.current_step = "Closed: your answers are saved; the job is queued"
                    job.pipeline_state = S.READY_TO_APPLY
                    app.requires_user_input, app.user_action_required = False, None
                    _event(db, job, app, keys, "Answers saved; the job is back in the queue")
                    requeued.append(str(job.id))
                    continue
                app.next_action = "Your answers are saved: resume the browser session"
                _event(db, job, app, keys, "Answers saved; ready to resume the browser session")
                requeued.append(str(job.id))
            continue
        before = job.pipeline_state
        apply_readiness(
            db, job, app, evaluate_readiness(db, settings, profile, job, app), actor="user"
        )
        if job.pipeline_state is not before:
            _event(db, job, app, keys, f"Answers saved; job is now {job.pipeline_state.value}")
            requeued.append(str(job.id))
    return requeued


def _event(db: Session, job: Job, app: Application, keys: list[str], reason: str) -> None:
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id,
            event_type="user_answered_questions",
            source=EventSource.USER,
            reason=reason,
            evidence={"question_keys": keys},  # keys only, never the answers
        )
    )


def update_saved_answer(db: Session, answer_id: uuid.UUID, answer: str) -> ApplicationAnswer:
    row = db.get(ApplicationAnswer, answer_id)
    if row is None or row.application_id is not None:
        raise NeedsInputError("No saved answer with this id")
    _, row.answer = _check(row.question_key, answer)
    row.source = USER_SOURCE.format(date=utcnow().date().isoformat())
    row.status = AnswerStatus.VERIFIED
    db.commit()
    return row


def delete_saved_answer(db: Session, answer_id: uuid.UUID) -> None:
    row = db.get(ApplicationAnswer, answer_id)
    if row is None or row.application_id is not None:
        raise NeedsInputError("No saved answer with this id")
    db.delete(row)
    db.commit()
