"""Needs my input: paused jobs, their questions, and the user's reusable saved answers."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.deps import profile_dep
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile
from backend.services.needs_input import (
    NeedsInputError,
    delete_saved_answer,
    paused_jobs,
    save_answers,
    saved_global_answers,
    update_saved_answer,
)
from database.session import get_db

router = APIRouter(prefix="/needs-input", tags=["needs-input"])
DB = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ProfileDep = Annotated[UserProfile, Depends(profile_dep)]


class AnswerIn(BaseModel):
    question_key: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9_]+$")
    question: str = Field("", max_length=2000)
    answer: str = Field(min_length=1, max_length=2000)


class AnswersIn(BaseModel):
    answers: list[AnswerIn] = Field(min_length=1, max_length=50)


class SavedAnswerUpdate(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


def _saved(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "question_key": row.question_key,
        "question": row.question,
        "answer": row.answer,  # shown only on this computer, so the user can check and edit it
        "source": row.source,
        "updated_at": row.updated_at,
    }


@router.get("")
def list_paused(db: DB, settings: SettingsDep, profile: ProfileDep) -> dict[str, Any]:
    """Every job waiting for the user, with its exact questions, plus the saved answers."""
    jobs = paused_jobs(db, settings, profile)
    return {
        "jobs": [j.as_dict() for j in jobs],
        "saved_answers": [_saved(a) for a in saved_global_answers(db)],
    }


@router.post("/answers")
def answer(body: AnswersIn, db: DB, settings: SettingsDep, profile: ProfileDep) -> dict[str, Any]:
    """Save answers once (reused for every application) and put waiting jobs back in queue."""
    try:
        return save_answers(db, settings, profile, [a.model_dump() for a in body.answers])
    except NeedsInputError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/saved-answers/{answer_id}")
def edit_saved(answer_id: uuid.UUID, body: SavedAnswerUpdate, db: DB) -> dict[str, Any]:
    try:
        return _saved(update_saved_answer(db, answer_id, body.answer))
    except NeedsInputError as exc:
        raise HTTPException(404 if "No saved" in str(exc) else 422, str(exc)) from exc


@router.delete("/saved-answers/{answer_id}", status_code=204)
def delete_saved(answer_id: uuid.UUID, db: DB) -> None:
    try:
        delete_saved_answer(db, answer_id)
    except NeedsInputError as exc:
        raise HTTPException(404, str(exc)) from exc
