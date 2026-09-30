import pytest

from backend.core.normalize import (
    canonicalize_url,
    detect_remote_type,
    extract_experience,
    extract_skills,
    html_to_text,
    job_fingerprint,
    normalize_company,
    normalize_title,
    title_matches,
)
from database.enums import RemoteType


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Technologies Pvt. Ltd.", "acme technologies"),
        ("  ACME   Inc ", "acme"),
        ("Orbit Data Ltd", "orbit data"),
        ("Orbit Data", "orbit data"),
    ],
)
def test_normalize_company(raw: str, expected: str) -> None:
    assert normalize_company(raw) == expected


def test_normalize_title_expands_abbreviations() -> None:
    assert normalize_title("Sr. Software Engg (Backend)") == "senior software engineer backend"
    assert normalize_title("Software Engineer - Backend") == normalize_title(
        "Software Engineer (Backend)"
    )


def test_canonical_url_drops_tracking_but_keeps_job_id() -> None:
    a = canonicalize_url("https://www.Example.com/jobs/1/?utm_source=x&gh_src=abc#apply")
    b = canonicalize_url("http://example.com/jobs/1")
    assert a == b == "https://example.com/jobs/1"
    # gh_jid identifies the job on company career pages; it must survive.
    c = canonicalize_url("https://acme.com/careers?gh_jid=123&utm_medium=li")
    d = canonicalize_url("https://acme.com/careers?gh_jid=456")
    assert c == "https://acme.com/careers?gh_jid=123"
    assert c != d
    assert canonicalize_url(None) is None
    assert canonicalize_url("not a url") is None


def test_fingerprint_ignores_cosmetic_differences() -> None:
    assert job_fingerprint("Orbit Data Ltd", "Data Engineer", "Noida, India") == job_fingerprint(
        "orbit data", "data  engineer", "Noida,  India"
    )
    assert job_fingerprint("Acme", "SWE", "Gurgaon") == job_fingerprint(
        "Acme", "Software Engineer", "Gurugram"
    )
    assert job_fingerprint("Acme", "Data Engineer", "Noida") != job_fingerprint(
        "Acme", "Data Engineer", "Pune"
    )


def test_html_to_text_unescapes_and_finds_requirements() -> None:
    html = (
        "&lt;p&gt;About us&amp;nbsp;&lt;/p&gt;&lt;h3&gt;Requirements&lt;/h3&gt;"
        "&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;"
        "&lt;li&gt;2+ years of experience&lt;/li&gt;&lt;/ul&gt;"
        "&lt;h3&gt;Benefits&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Snacks&lt;/li&gt;&lt;/ul&gt;"
    )
    text, requirements = html_to_text(html)
    assert text is not None and "About us" in text and "<" not in text
    assert requirements == ["Python", "2+ years of experience"]
    assert html_to_text(None) == (None, [])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Requires 3+ years of experience in Go", "3+ years"),
        ("2-4 years of relevant experience", "2-4 years"),
        ("up to 1 year of experience", "1 year"),
        ("Open to freshers", "freshers"),
        ("Great team, great culture", None),
        (None, None),
    ],
)
def test_extract_experience_quotes_only_what_is_written(
    text: str | None, expected: str | None
) -> None:
    assert extract_experience(text) == expected


def test_remote_type_defaults_to_unknown_not_onsite() -> None:
    assert detect_remote_type("Gurugram, India") is RemoteType.UNKNOWN
    assert detect_remote_type("Remote - India") is RemoteType.REMOTE
    assert detect_remote_type("Hybrid - Noida") is RemoteType.HYBRID
    assert detect_remote_type(None, "Onsite Engineer") is RemoteType.ONSITE


def test_extract_skills_whole_words_only() -> None:
    text = "We use Go, C++ and PostgreSQL at Google. Java experience a plus; JavaScript not needed."
    found = extract_skills(text, ["Go", "C++", "PostgreSQL", "Java", "JavaScript", "Rust", "SQL"])
    assert found == ["Go", "C++", "PostgreSQL", "Java", "JavaScript"]  # no Rust, no bare SQL


def test_title_matching_uses_aliases_and_whole_phrases() -> None:
    aliases = {"ML Engineer": ["Machine Learning Engineer"], "SDE": ["SDE II"]}
    assert title_matches("Senior Machine Learning Engineer", ["ML Engineer"], aliases)
    assert title_matches("SDE 1", ["SDE"], aliases)
    assert title_matches("Backend Software Engineer", ["Software Engineer"], {})
    assert not title_matches(
        "Software Sales Development Representative", ["Software Developer"], {}
    )
    assert not title_matches("Account Executive", ["Software Engineer"], aliases)
