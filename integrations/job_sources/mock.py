"""Offline mock source backed by fixtures. Realistic but clearly fictional (*.invalid URLs)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.core.normalize import (
    detect_remote_type,
    extract_experience,
    extract_skills,
    html_to_text,
)
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceError,
)

DEFAULT_FIXTURES = Path(__file__).parent / "fixtures" / "mock_jobs.json"


class MockSource(JobSource):
    name = "mock"

    def __init__(
        self,
        *,
        skills_vocabulary: list[str],
        fixtures: list[dict[str, Any]] | None = None,
        fail_boards: set[str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if fixtures is None:
            fixtures = json.loads(DEFAULT_FIXTURES.read_text(encoding="utf-8"))
        self.fixtures = fixtures
        self.skills_vocabulary = skills_vocabulary
        self.fail_boards = fail_boards or set()
        self._now = now

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        boards = sorted({f["board"] for f in self.fixtures})
        return [SearchTask(label=f"Mock board {b}", params={"board": b}) for b in boards]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        board = task.params["board"]
        if board in self.fail_boards:
            raise SourceError(f"Mock board {board} is configured to fail")
        return [
            RawJob(source_job_id=f["id"], payload=f) for f in self.fixtures if f["board"] == board
        ]

    def get_job(self, source_job_id: str) -> RawJob | None:
        match = next((f for f in self.fixtures if f["id"] == source_job_id), None)
        return RawJob(source_job_id=source_job_id, payload=match) if match else None

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p = raw.payload
        description, requirements = html_to_text(p.get("description_html"))
        salary = p.get("salary") or {}
        hours = p.get("posted_hours_ago")
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=p.get("company"),
            title=p.get("title") or "",
            location=p.get("location"),
            remote_type=detect_remote_type(p.get("location"), p.get("title")),
            salary_min=salary.get("min"),
            salary_max=salary.get("max"),
            salary_currency=salary.get("currency"),
            experience_required=extract_experience(description),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=p.get("url") or "",
            application_url=p.get("url"),
            posted_at=self._now() - timedelta(hours=hours) if hours is not None else None,
            posted_at_kind="published" if hours is not None else "unknown",
            raw={"id": p.get("id"), "case": p.get("case")},
        )
