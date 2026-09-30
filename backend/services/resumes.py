"""Read-only access to the master resume. This module never writes, copies or edits it."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from backend.core.config import Settings

SUPPORTED = {".pdf", ".md", ".txt"}
MAX_TEXT_CHARS = 20_000


@dataclass
class MasterResume:
    path: Path
    source: str  # MASTER_RESUME_PATH | resumes/master
    sha256: str
    text: str


class ResumeUnavailableError(RuntimeError):
    pass


def locate_master_resume(settings: Settings) -> tuple[Path, str] | None:
    if settings.master_resume_path is not None:
        path = settings.master_resume_path
        return (path, "MASTER_RESUME_PATH") if path.is_file() else None
    folder = settings.resumes_dir / "master"
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    return (files[0], "resumes/master") if files else None


def read_master_resume(settings: Settings) -> MasterResume:
    found = locate_master_resume(settings)
    if found is None:
        where = settings.master_resume_path or (settings.resumes_dir / "master")
        raise ResumeUnavailableError(
            f"No master resume found at {where}. Put it in resumes/master/ "
            "or set MASTER_RESUME_PATH (PDF, Markdown or text)."
        )
    path, source = found
    data = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    else:
        text = data.decode("utf-8", errors="replace")
    return MasterResume(
        path=path,
        source=source,
        sha256=hashlib.sha256(data).hexdigest(),
        text=text.strip()[:MAX_TEXT_CHARS],
    )
