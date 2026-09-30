"""The master resume is readable by the agent and can never be modified by it."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from agent.tools import ToolContext, build_default_registry
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile
from database.enums import ToolPermission

ALL = frozenset(ToolPermission)  # even with every permission, nothing may touch the resume
PATH_LIKE = {"path", "file", "file_path", "filename", "filepath", "resume_path", "directory"}


def fingerprint(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_size


def test_configured_master_resume_is_readable_and_unchanged(
    db: Session, profile: UserProfile
) -> None:
    """Runs against the real MASTER_RESUME_PATH from .env (skipped where none is set)."""
    settings = get_settings()
    path = settings.master_resume_path
    if path is None or not path.is_file():
        pytest.skip("MASTER_RESUME_PATH is not configured on this machine")
    before = fingerprint(path)
    ctx = ToolContext(db=db, settings=settings, profile=profile, allowed=ALL)
    result = build_default_registry().execute("get_master_resume", {}, ctx)
    assert result.ok, result.message
    data = result.data or {}
    assert data["source"] == "MASTER_RESUME_PATH"
    assert data["file_name"] == path.name
    assert data["sha256"] == before[0]
    assert len(data["text"]) > 200  # real content was extracted
    assert fingerprint(path) == before  # content hash, timestamp and size unchanged


def test_no_tool_accepts_a_file_path() -> None:
    """The agent cannot aim any tool at the resume file: no tool takes a path argument."""
    offenders = {
        t.name: sorted(set(t.input_schema().get("properties", {})) & PATH_LIKE)
        for t in build_default_registry().tools()
    }
    assert {k: v for k, v in offenders.items() if v} == {}


@pytest.fixture
def protected_resume(tmp_path: Path) -> Path:
    resume = tmp_path / "master" / "Resume.md"
    resume.parent.mkdir()
    resume.write_text(
        "# Test Candidate\nPython developer, verified facts only.\n", encoding="utf-8"
    )
    os.chmod(resume, stat.S_IREAD)  # OS read-only: any write attempt would raise
    yield resume
    os.chmod(resume, stat.S_IREAD | stat.S_IWRITE)


def test_every_tool_leaves_the_master_resume_untouched(
    db: Session, profile: UserProfile, tmp_path: Path, protected_resume: Path,
    shortlisted_job_id: Any,
) -> None:  # fmt: skip
    settings = Settings(master_resume_path=protected_resume, export_dir=tmp_path / "exports",
                        resumes_dir=tmp_path / "resumes", analyzer_backend="heuristic")  # fmt: skip
    before = fingerprint(protected_resume)
    ctx = ToolContext(db=db, settings=settings, profile=profile, allowed=ALL)
    registry = build_default_registry()

    read = registry.execute("get_master_resume", {}, ctx)
    assert read.ok and (read.data or {})["text"].startswith("# Test Candidate")

    job = str(shortlisted_job_id)
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_resume_versions", {}),
        ("create_tailored_resume", {"job_id": job}),
        ("create_application_draft", {"job_id": job}),
        ("update_application_status", {"job_id": job, "target_status": "WITHDRAWN", "reason": "t"}),
        ("export_application_data", {"format": "json"}),
        ("get_application_statistics", {}),
        ("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}),
    ]
    for name, args in calls:
        registry.execute(name, args, ctx)
    assert fingerprint(protected_resume) == before
    assert not any((tmp_path / "master").glob("*.json"))  # exports never land beside it
