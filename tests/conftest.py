"""Shared fixtures. Tests run on in-memory SQLite; nothing touches real services."""

import os
from collections.abc import Iterator

# Force safe settings before any app module reads them.
os.environ["DEVELOPMENT_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ.pop("ANTHROPIC_API_KEY", None)
# Tests never call Claude: the deterministic heuristic analyzer (or fakes) are used instead.
os.environ["ANALYZER_BACKEND"] = "heuristic"
# The in-process daily scheduler never starts in tests; tests drive it directly.
os.environ["DAILY_RUN_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import database.models  # noqa: F401
from backend.api import deps
from backend.core.config import get_settings
from backend.core.profile import UserProfile
from backend.main import create_app
from backend.services.shortlist import ShortlistRules
from database.base import Base
from database.session import get_db
from integrations.job_sources.config import JobSourcesConfig, load_job_sources_config
from integrations.job_sources.mock import MockSource


@pytest.fixture
def engine() -> Iterator[Engine]:
    from database.session import build_engine

    # The app's own SQLite setup (foreign keys, busy timeout, savepoints), in memory.
    eng = build_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def sources_config() -> JobSourcesConfig:
    """The real config/job_sources.yaml, so tests exercise the shipped search settings."""
    return load_job_sources_config(get_settings().job_sources_path)


@pytest.fixture
def profile() -> UserProfile:
    """A fictional fresher. Facts and preferences are deliberately separate."""
    return UserProfile.model_validate(
        {
            "verified_profile": {
                "name": "Test Candidate",
                "email": "candidate@example.com",
                "total_experience_years": 0,
                "education": [
                    {
                        "institution": "Example Institute",
                        "degree": "B.Tech",
                        "field_of_study": "Computer Science",
                        "graduation_year": 2026,
                    }
                ],
                "skills": [
                    "Python",
                    "SQL",
                    "FastAPI",
                    "Docker",
                    "Java",
                    "Data Structures",
                    "Algorithms",
                ],
                "projects": [
                    {"name": "Pipeline", "description": "ETL demo", "technologies": ["Airflow"]}
                ],
            },
            "preferences": {
                "preferred_roles": [
                    "Software Engineer",
                    "Software Developer",
                    "SDE",
                    "AI Engineer",
                    "ML Engineer",
                    "Data Engineer",
                    "Data Scientist",
                    "Backend Engineer",
                ],
                "preferred_locations": ["Gurugram", "Noida", "Delhi NCR", "Remote"],
                "minimum_salary": 1000000,
                "maximum_application_age_hours": 48,
            },
        }
    )


@pytest.fixture
def rules() -> ShortlistRules:
    return ShortlistRules()


@pytest.fixture
def mock_source(sources_config: JobSourcesConfig) -> MockSource:
    return MockSource(skills_vocabulary=sources_config.skills_vocabulary)


@pytest.fixture
def client(
    engine: Engine,
    session_factory: sessionmaker[Session],
    profile: UserProfile,
    rules: ShortlistRules,
    sources_config: JobSourcesConfig,
) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    def _build_source(name: str):  # type: ignore[no-untyped-def]
        if name == "mock":
            return MockSource(skills_vocabulary=sources_config.skills_vocabulary)
        raise ValueError(f"Source {name!r} is not available in tests")

    app = create_app()
    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[deps.session_factory_dep] = lambda: session_factory
    app.dependency_overrides[deps.profile_dep] = lambda: profile
    app.dependency_overrides[deps.rules_dep] = lambda: rules
    app.dependency_overrides[deps.source_builder_dep] = lambda: _build_source
    with TestClient(app) as c:
        yield c


# --- Phase 3: agent fixtures -------------------------------------------------------------

from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from agent.claude_client import ClaudeCallError, StructuredReply  # noqa: E402
from backend.core.config import Settings  # noqa: E402


class FakeClaude:
    """Scripted stand-in for Claude. Each queued item is a decision dict, raw text, an
    exception to raise, or a callable(user_prompt) -> one of those."""

    name = "claude_cli"
    model = "fake-claude"

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, system: str, user: str, schema: dict[str, Any]) -> StructuredReply:
        self.prompts.append(user)
        if not self.replies:
            raise ClaudeCallError("FakeClaude has no scripted replies left")
        item = self.replies.pop(0)
        if callable(item) and not isinstance(item, type):
            item = item(user)
        if isinstance(item, Exception):
            raise item
        return StructuredReply(payload=item, model=self.model)


def decision(
    action: str = "call_tool",
    tool: str = "",
    message: str = "",
    summary: str = "step",
    **arguments: Any,
) -> dict[str, Any]:
    import json as _json

    return {
        "summary": summary,
        "action": action,
        "tool": tool,
        "arguments_json": _json.dumps(arguments, default=str),
        "message": message,
    }


@pytest.fixture
def agent_settings(tmp_path: Path) -> Settings:
    """Isolated dirs: no real resume, exports go to a temp folder."""
    (tmp_path / "resumes" / "master").mkdir(parents=True)
    return Settings(
        resumes_dir=tmp_path / "resumes",
        export_dir=tmp_path / "exports",
        master_resume_path=None,
        analyzer_backend="heuristic",
    )


@pytest.fixture
def seeded(
    session_factory: sessionmaker[Session],
    mock_source: MockSource,
    sources_config: JobSourcesConfig,
) -> None:
    from backend.services.discovery import DiscoveryService, create_discovery_run

    query = sources_config.build_query([], [])
    with session_factory() as s:
        run = create_discovery_run(s, mock_source, query, dev_mode=True)
    DiscoveryService(session_factory).execute(run.id, mock_source, query)


@pytest.fixture
def shortlisted_job_id(
    seeded: None,
    session_factory: sessionmaker[Session],
    profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> Any:
    """mock-001 analyzed with the heuristic analyzer -> SHORTLISTED."""
    from sqlalchemy import select

    from agent.analysis.analyzers import HeuristicAnalyzer
    from backend.services.analysis import AnalysisContext, analyze_single
    from database.models import Job

    with session_factory() as s:
        job = s.scalar(select(Job).where(Job.source_job_id == "mock-001"))
        assert job is not None
        analyzer = HeuristicAnalyzer(
            sources_config.search.location_patterns, sources_config.search.term_aliases
        )
        analyze_single(
            s,
            job,
            AnalysisContext(analyzer=analyzer, profile=profile, rules=ShortlistRules()),
            dev_mode=True,
        )
        assert job.pipeline_state.value == "SHORTLISTED"
        return job.id


# --- Phase 4: tailoring fixtures -------------------------------------------------------

MASTER_RESUME_MD = """Test Candidate
Summary
Software developer with backend experience (Python, FastAPI) and AI/ML projects.
Technical Skills
Languages: Python, Java, SQL, C#
Frameworks: FastAPI, ASP.NET, PyTorch
Databases: PostgreSQL, MongoDB
Cloud & Data: Azure Data Factory
AI/ML: CNN, BiLSTM, LLMs, Go
Tools: Git, Docker
Experience
Software Developer May 2026 - Present
Acme AI Remote
- Building backend services and REST APIs with FastAPI.
- Integrating LLM features into production systems.
Backend Engineering Intern Jun 2025 - Sep 2025
DataCo Noida
- Built data pipelines with Azure Data Factory.
- Developed backend APIs using C# and ASP.NET.
Projects
Retail Demand Forecaster | Python, PyTorch, CNN
- Built a CNN + BiLSTM model achieving 91.40% accuracy; deployed via Streamlit.
Leave Portal | React, Node.js, MongoDB
- Full-stack app with 10+ REST APIs.
Education
Example Institute of Technology, Bachelor of Technology in Computer Science, 2022 - 2026, CGPA 7.90
Publications
Demand Forecasting with CNN-LSTM Hybrids | Example Conference 2026
"""


@pytest.fixture
def tailor_profile() -> UserProfile:
    """Fictional candidate whose facts match MASTER_RESUME_MD. Go is on the resume but unconfirmed."""
    return UserProfile.model_validate(
        {
            "verified_profile": {
                "name": "Test Candidate",
                "email": "candidate@example.com",
                "phone": "+91-00000",
                "location": "Gurugram, India",
                "total_experience_years": 0.4,
                "education": [
                    {
                        "institution": "Example Institute of Technology",
                        "degree": "Bachelor of Technology",
                        "field_of_study": "Computer Science",
                        "graduation_year": 2026,
                        "grade": "CGPA 7.90",
                    }
                ],
                "skills": [
                    "Python",
                    "Java",
                    "SQL",
                    "C#",
                    "FastAPI",
                    "ASP.NET",
                    "PyTorch",
                    "PostgreSQL",
                    "MongoDB",
                    "Azure Data Factory",
                    "CNN",
                    "BiLSTM",
                    "LLMs",
                    "Go",
                    "Git",
                    "Docker",
                ],
                "unconfirmed_skills": ["Go"],
                "projects": [
                    {
                        "name": "Retail Demand Forecaster",
                        "technologies": ["Python", "PyTorch", "CNN"],
                        "description": "Built a CNN + BiLSTM model achieving 91.40% accuracy; deployed via Streamlit.",
                    },
                    {
                        "name": "Leave Portal",
                        "technologies": ["React", "Node.js", "MongoDB"],
                        "description": "Full-stack app with 10+ REST APIs.",
                    },
                ],
                "work_experience": [
                    {
                        "company": "Acme AI",
                        "title": "Software Developer",
                        "start_date": "2026-05",
                        "end_date": None,
                        "location": "Remote",
                        "highlights": [
                            "Building backend services and REST APIs with FastAPI.",
                            "Integrating LLM features into production systems.",
                        ],
                    },
                    {
                        "company": "DataCo",
                        "title": "Backend Engineering Intern",
                        "start_date": "2025-06",
                        "end_date": "2025-09",
                        "location": "Noida",
                        "highlights": [
                            "Built data pipelines with Azure Data Factory.",
                            "Developed backend APIs using C# and ASP.NET.",
                        ],
                    },
                ],
                "verified_answers": {
                    "work_authorization": "Yes",
                    "notice_period": "Immediate",
                    "expected_salary": "10 LPA",
                    "willing_to_relocate": "Yes",
                },
            },
            "preferences": {
                "preferred_roles": ["Software Engineer", "Backend Engineer"],
                "preferred_locations": ["Gurugram", "Remote"],
                "minimum_salary": 1000000,
            },
        }
    )


@pytest.fixture
def master_file(tmp_path: Path) -> Iterator[Path]:
    """Read-only master resume; teardown proves it was never modified."""
    import hashlib
    import stat

    path = tmp_path / "prv" / "Resume.md"
    path.parent.mkdir()
    path.write_text(MASTER_RESUME_MD, encoding="utf-8")
    os.chmod(path, stat.S_IREAD)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    yield path
    after = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert after == before, "master resume was modified"


@pytest.fixture
def tailor_settings(tmp_path: Path, master_file: Path) -> Settings:
    return Settings(master_resume_path=master_file, resumes_dir=tmp_path / "resumes",
                    export_dir=tmp_path / "exports", analyzer_backend="heuristic")  # fmt: skip


def good_resume(**overrides: Any) -> dict[str, Any]:
    """A tailored resume that is fully supported by MASTER_RESUME_MD."""
    base: dict[str, Any] = {
        "section_order": ["summary", "skills", "experience", "projects", "education", "publications"],
        "summary": "Backend-focused software developer building REST APIs with Python and FastAPI.",
        "skills": [{"category": "Backend", "items": ["Python", "FastAPI", "PostgreSQL", "Docker"]},
                   {"category": "Other", "items": ["SQL", "Java", "Git"]}],
        "experience": [
            {"company": "Acme AI", "title": "Software Developer", "start_date": "May 2026",
             "end_date": "Present", "location": "Remote",
             "bullets": ["Build backend services and REST APIs with FastAPI.",
                         "Integrate LLM features into production systems."]},
            {"company": "DataCo", "title": "Backend Engineering Intern",
             "start_date": "2025-06", "end_date": "2025-09", "location": "Noida",
             "bullets": ["Developed backend APIs using C# and ASP.NET."]},
        ],
        "projects": [{"name": "Retail Demand Forecaster", "technologies": ["Python", "PyTorch"],
                      "bullets": ["Built a CNN + BiLSTM model reaching 91.40% accuracy."]}],
        "education": [{"institution": "Example Institute of Technology",
                       "degree": "Bachelor of Technology", "field_of_study": "Computer Science",
                       "graduation_year": "2026", "grade": "CGPA 7.90"}],
        "certifications": [],
        "publications": ["Demand Forecasting with CNN-LSTM Hybrids"],
        "keywords_emphasized": ["Python", "FastAPI", "REST APIs"],
        "change_notes": ["Moved backend skills first", "Dropped the Leave Portal project"],
    }  # fmt: skip
    base.update(overrides)
    return base
