"""Resume versions: master status, tailored version list/detail and artifact download.

Read-only. Tailored artifacts are served only from resumes/tailored/. The master resume file
is served by one endpoint for preview/download (REQUIREMENTS 19.2.8): exactly the file PIE
located, never a path from the request. The API listens on 127.0.0.1 only.
"""

import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.deps import profile_dep
from backend.api.schemas import (
    MasterResumeStatus,
    ResumeUse,
    ResumeVersionBrief,
    ResumeVersionDetail,
)
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile
from backend.services.resumes import (
    ResumeUnavailableError,
    locate_master_resume,
    read_master_resume,
)
from backend.services.statuses import user_status
from backend.services.tailoring import resume_lock_reason
from database.models import Application, Job, ResumeVersion
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


def uses(db: Session, v: ResumeVersion) -> list[ResumeUse]:
    """Applications linked to this version, each with whether it locks the resume."""
    apps = db.scalars(select(Application).where(Application.resume_version_id == v.id)).all()
    return [
        ResumeUse(
            application_id=a.id,
            job_id=a.job_id,
            title=a.job.title,
            company=a.job.company.name if a.job.company else None,
            user_status=user_status(a.job.pipeline_state),
            locked=resume_lock_reason(db, a.job, a) is not None,
        )
        for a in apps
    ]


def brief(db: Session, v: ResumeVersion) -> ResumeVersionBrief:
    used = uses(db, v)
    return ResumeVersionBrief.of(v).model_copy(
        update={"used_by": used, "locked": any(u.locked for u in used)}
    )


def version_detail(db: Session, v: ResumeVersion) -> ResumeVersionDetail:
    job = db.get(Job, v.job_id) if v.job_id else None
    paths = artifact_paths(v)
    markdown = paths["markdown"].read_text(encoding="utf-8") if "markdown" in paths else None
    return ResumeVersionDetail(
        **brief(db, v).model_dump(),
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
    return [brief(db, v) for v in db.scalars(stmt).all()]


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


@router.get("/master/file")
def master_file(settings: Annotated[Settings, Depends(get_settings)]) -> FileResponse:
    """The master resume itself, for preview/download on this computer (read only)."""
    found = locate_master_resume(settings)
    if found is None:
        raise HTTPException(404, "No master resume found")
    path = found[0]
    media = {
        ".pdf": "application/pdf",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path, media_type=media, filename=path.name, content_disposition_type="inline"
    )


@router.get("/versions/{version_id}/ats")
def ats_check(
    version_id: uuid.UUID,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
) -> dict[str, Any]:
    """Local ATS check of a tailored resume against its job (offline; nothing is uploaded)."""
    from backend.services.ats import check_resume

    v = db.get(ResumeVersion, version_id)
    if v is None:
        raise HTTPException(404, "Resume version not found")
    pdf = artifact_paths(v).get("pdf")
    if pdf is None:
        raise HTTPException(409, "Only a tailored resume with a PDF can be checked")
    tailored_root = (settings.resumes_dir / "tailored").resolve()
    if tailored_root not in pdf.resolve().parents:
        raise HTTPException(404, "Artifact not available")
    return check_resume(db, v, pdf, profile).as_dict()
