"""Resume versions: master status, tailored version list/detail and artifact download.

Read-only. Artifacts are served only from resumes/tailored/ (never the master resume file).
"""

import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.schemas import MasterResumeStatus, ResumeVersionBrief, ResumeVersionDetail
from backend.core.config import Settings, get_settings
from backend.services.resumes import ResumeUnavailableError, read_master_resume
from database.models import Job, ResumeVersion
from database.session import get_db

router = APIRouter(prefix="/resumes", tags=["resumes"])
DB = Annotated[Session, Depends(get_db)]


def artifact_paths(v: ResumeVersion) -> dict[str, Path]:
    """Artifacts of a tailored version: the primary file plus its Markdown twin."""
    if v.is_master:
        return {}
    primary = Path(v.file_path)
    paths = {"markdown": primary.with_suffix(".md")}
    if primary.suffix == ".pdf":
        paths["pdf"] = primary
    return {k: p for k, p in paths.items() if p.is_file()}


def version_detail(db: Session, v: ResumeVersion) -> ResumeVersionDetail:
    job = db.get(Job, v.job_id) if v.job_id else None
    paths = artifact_paths(v)
    markdown = paths["markdown"].read_text(encoding="utf-8") if "markdown" in paths else None
    return ResumeVersionDetail(
        **ResumeVersionBrief.of(v).model_dump(),
        job_title=job.title if job else None,
        content=v.content,
        change_details=v.change_details,
        tailoring_report=v.tailoring_report,
        validation_report=v.validation_report,
        source_version_id=v.source_version_id,
        source_sha256=v.source_sha256,
        model=v.model,
        prompt_version=v.prompt_version,
        artifacts=sorted(paths),
        markdown=markdown,
    )


@router.get("/master", response_model=MasterResumeStatus)
def master_status(settings: Annotated[Settings, Depends(get_settings)]) -> MasterResumeStatus:
    try:
        master = read_master_resume(settings)
    except ResumeUnavailableError as exc:
        return MasterResumeStatus(
            available=False, file_name=None, source=None, sha256=None, detail=str(exc)
        )
    return MasterResumeStatus(
        available=True,
        file_name=master.path.name,
        source=master.source,
        sha256=master.sha256,
        detail=None,
    )


@router.get("/versions", response_model=list[ResumeVersionBrief])
def list_versions(
    db: DB, job_id: uuid.UUID | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[ResumeVersionBrief]:
    stmt = select(ResumeVersion).order_by(ResumeVersion.created_at.desc()).limit(limit)
    if job_id is not None:
        stmt = stmt.where(ResumeVersion.job_id == job_id)
    return [ResumeVersionBrief.of(v) for v in db.scalars(stmt).all()]


@router.get("/versions/{version_id}", response_model=ResumeVersionDetail)
def get_version(version_id: uuid.UUID, db: DB) -> ResumeVersionDetail:
    v = db.get(ResumeVersion, version_id)
    if v is None:
        raise HTTPException(404, "Resume version not found")
    return version_detail(db, v)


@router.get("/versions/{version_id}/artifact")
def download_artifact(
    version_id: uuid.UUID,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    format: Literal["pdf", "markdown"] = "pdf",
) -> FileResponse:
    v = db.get(ResumeVersion, version_id)
    if v is None:
        raise HTTPException(404, "Resume version not found")
    path = artifact_paths(v).get(format)
    if path is None:
        raise HTTPException(404, f"No {format} artifact for this version")
    tailored_root = (settings.resumes_dir / "tailored").resolve()
    if tailored_root not in path.resolve().parents:  # only ever serve generated artifacts
        raise HTTPException(404, "Artifact not available")
    media = "application/pdf" if format == "pdf" else "text/markdown; charset=utf-8"
    return FileResponse(path, media_type=media, filename=path.name)
