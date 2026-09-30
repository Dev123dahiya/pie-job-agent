"""Grounding validation of tailored resumes against the master resume + verified profile."""

from typing import Any

import pytest

from agent.tailoring.schema import TailoringOutputError, parse_tailored
from agent.tailoring.validation import Evidence, validate_tailored
from backend.core.profile import UserProfile
from tests.conftest import MASTER_RESUME_MD, good_resume


@pytest.fixture
def evidence(tailor_profile: UserProfile) -> Evidence:
    return Evidence(MASTER_RESUME_MD, tailor_profile, ["Kubernetes", "Django", "Go", "Kafka"])


def codes(payload: dict[str, Any], evidence: Evidence) -> set[str]:
    return {v.code for v in validate_tailored(parse_tailored(payload), evidence)}


def exp(**over: Any) -> list[dict[str, Any]]:
    entries = good_resume()["experience"]
    entries[0] = {**entries[0], **over}
    return entries


def test_supported_resume_passes(evidence: Evidence) -> None:
    assert validate_tailored(parse_tailored(good_resume()), evidence) == []


def test_formats_and_master_text_skills_are_accepted(evidence: Evidence) -> None:
    ok = good_resume(skills=[{"category": "Data", "items": ["Azure Data Factory", "NodeJS"]}])
    ok["experience"][1] |= {"start_date": "Jun 2025", "end_date": "09/2025"}
    assert codes(ok, evidence) == set()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        # Go is on the resume but UNCONFIRMED: rejected everywhere.
        (
            good_resume(skills=[{"category": "Languages", "items": ["Python", "Go"]}]),
            "UNCONFIRMED_SKILL",
        ),
        (good_resume(summary="Backend developer using Go and Python."), "UNCONFIRMED_SKILL"),
        (good_resume(summary="Backend developer comfortable with Golang."), "UNCONFIRMED_SKILL"),
        (
            good_resume(
                projects=[
                    {"name": "Retail Demand Forecaster", "technologies": ["Go"], "bullets": []}
                ]
            ),
            "UNCONFIRMED_SKILL",
        ),
        # Unsupported technologies / skills (including job requirements copied over).
        (good_resume(skills=[{"category": "Cloud", "items": ["Kubernetes"]}]), "UNSUPPORTED_SKILL"),
        (good_resume(skills=[{"category": "Web", "items": ["Django"]}]), "UNSUPPORTED_SKILL"),
        (good_resume(summary="Deployed services on Kubernetes."), "UNSUPPORTED_TECHNOLOGY"),
        (
            good_resume(experience=exp(bullets=["Streamed events through Kafka."])),
            "UNSUPPORTED_TECHNOLOGY",
        ),
        # Employers, titles, dates.
        (good_resume(experience=exp(company="Google")), "UNSUPPORTED_EMPLOYER"),
        (good_resume(experience=exp(title="Senior Software Engineer")), "UNSUPPORTED_TITLE"),
        (good_resume(experience=exp(start_date="Jan 2025")), "UNSUPPORTED_DATES"),
        (good_resume(experience=exp(end_date="Aug 2026")), "UNSUPPORTED_DATES"),
        # Metrics.
        (good_resume(summary="Cut API latency by 40% for 1M users."), "UNSUPPORTED_METRIC"),
        (good_resume(experience=exp(bullets=["Shipped 12 REST APIs."])), "UNSUPPORTED_METRIC"),
        (good_resume(summary="Developer with 3 years of experience."), "UNSUPPORTED_METRIC"),
        # Projects, education, certifications, publications.
        (
            good_resume(projects=[{"name": "Chat App", "technologies": ["Python"], "bullets": []}]),
            "UNSUPPORTED_PROJECT",
        ),
        (
            good_resume(
                projects=[{"name": "Leave Portal", "technologies": ["Docker"], "bullets": []}]
            ),
            "UNSUPPORTED_TECHNOLOGY",
        ),
        (
            good_resume(
                education=[
                    {
                        "institution": "Example Institute of Technology",
                        "degree": "Master of Science",
                        "field_of_study": "Computer Science",
                        "graduation_year": "2026",
                        "grade": "",
                    }
                ]
            ),
            "UNSUPPORTED_EDUCATION",
        ),
        (
            good_resume(
                education=[
                    {
                        "institution": "Example Institute of Technology",
                        "degree": "Bachelor of Technology",
                        "field_of_study": "Computer Science",
                        "graduation_year": "2025",
                        "grade": "",
                    }
                ]
            ),
            "UNSUPPORTED_DATES",
        ),
        (
            good_resume(
                education=[
                    {
                        "institution": "Harvard University",
                        "degree": "",
                        "field_of_study": "",
                        "graduation_year": "",
                        "grade": "",
                    }
                ]
            ),
            "UNSUPPORTED_EDUCATION",
        ),
        (good_resume(certifications=["AWS Certified Developer"]), "UNSUPPORTED_CERTIFICATION"),
        (good_resume(publications=["Scaling Transformers to 1B Users"]), "UNSUPPORTED_PUBLICATION"),
    ],
)
def test_unsupported_claims_rejected(
    payload: dict[str, Any], code: str, evidence: Evidence
) -> None:
    assert code in codes(payload, evidence)


def test_project_technology_from_its_description_is_allowed(evidence: Evidence) -> None:
    ok = good_resume(
        projects=[
            {
                "name": "Retail Demand Forecaster",
                "technologies": ["Python", "Streamlit"],
                "bullets": [],
            }
        ]
    )
    assert codes(ok, evidence) == set()


def test_malformed_structure_is_rejected() -> None:
    with pytest.raises(TailoringOutputError):
        parse_tailored("{not json")
    with pytest.raises(TailoringOutputError, match="header"):
        parse_tailored(good_resume(header={"name": "Someone Else"}))
    with pytest.raises(TailoringOutputError):
        parse_tailored({k: v for k, v in good_resume().items() if k != "experience"})
