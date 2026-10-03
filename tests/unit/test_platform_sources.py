"""C3 / D: Naukri and LinkedIn search sources, with a fake browser (no site is contacted)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from integrations.job_sources.base import SearchQuery, SourceAuthRequired, SourceError
from integrations.job_sources.platforms import LinkedInSource, NaukriSource
from integrations.platforms.base import load_status, parse_relative_age

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class FakeBrowser:
    def __init__(self, pages: list[dict[str, Any]], detail: str = "Full job description") -> None:
        self.pages = pages
        self.detail = detail
        self.opened: list[str] = []
        self.render_all: list[bool] = []

    def read_cards(self, url: str, spec: dict[str, Any], *, render_all: bool = False) -> Any:
        self.opened.append(url)
        self.render_all.append(render_all)
        return self.pages.pop(0)

    def read_page_text(self, url: str) -> Any:
        self.opened.append(url)
        return {"status": 200, "url": url, "text": self.detail}


def naukri_card(i: int, **over: Any) -> dict[str, Any]:
    return {
        "id": f"0110260087{i:02d}",
        "title": "Python Developer",
        "url": f"https://www.naukri.com/job-listings-python-developer-acme-{i}?src=srp",
        "company": "Acme",
        "experience": "0-2 Yrs",
        "salary": "Not disclosed",
        "location": "Gurugram",
        "snippet": "Build APIs with Python and FastAPI",
        "tags": "python\nfastapi",
        "posted": "2 days ago",
        **over,
    }


def query() -> SearchQuery:
    return SearchQuery(terms=["Python Developer", "Backend Developer"],
                       locations=["Gurugram", "Noida"], max_age_hours=48)  # fmt: skip


def source(cls: Any, browser: FakeBrowser, tmp_path: Path, **kw: Any) -> Any:
    sleeps: list[float] = []
    src = cls(browser=browser, skills_vocabulary=["Python", "FastAPI"], data_dir=tmp_path,
              sleep=sleeps.append, now=lambda: NOW, **kw)  # fmt: skip
    src.sleeps = sleeps
    return src


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        ("2 days ago", 72),
        ("Posted 5 hours ago", 6),
        ("30+ days ago", 31 * 24),
        ("1 week ago", 14 * 24),
        ("Just now", 24),
        ("Today", 24),
    ],
)
def test_relative_ages_are_read_as_the_oldest_they_can_be(text: str, hours: int) -> None:
    assert parse_relative_age(text, NOW) == NOW - timedelta(hours=hours)


def test_relative_age_without_an_age_is_unknown() -> None:
    assert parse_relative_age("Promoted · Easy Apply", NOW) is None


def test_naukri_plans_few_searches_with_the_experience_filter(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [{"status": 200, "url": "https://www.naukri.com/x", "text": "", "items": []}]
    )
    src = source(NaukriSource, browser, tmp_path, max_searches=3, experience_years=lambda: 1.0)
    tasks = src.plan(query())
    assert len(tasks) == 3  # capped, not 2 x 2 x pages
    src.search_jobs(tasks[0], query())
    assert browser.opened == [
        "https://www.naukri.com/python-developer-jobs-in-gurugram?experience=1&jobAge=2"
    ]


def test_naukri_cards_become_jobs(tmp_path: Path) -> None:
    page = {
        "status": 200,
        "url": "https://www.naukri.com/s",
        "text": "jobs",
        "items": [naukri_card(1), naukri_card(2, title=""), naukri_card(3, id=None)],
    }
    browser = FakeBrowser([page])
    src = source(NaukriSource, browser, tmp_path)
    raws = src.search_jobs(src.plan(query())[0], query())
    assert [r.source_job_id for r in raws] == ["011026008701"]  # no title / no id: skipped
    job = src.normalize_job(raws[0])
    assert job.source == "naukri" and job.company == "Acme" and job.experience_required == "0-2 Yrs"
    assert job.url == "https://www.naukri.com/job-listings-python-developer-acme-1"  # no tracking
    assert job.posted_at_kind == "relative_upper_bound"
    assert job.posted_at is not None and job.posted_at < datetime.now(UTC) - timedelta(days=2)
    assert {"Python", "FastAPI"} <= set(job.skills)
    assert job.raw and job.raw["salary_text"] == "Not disclosed"  # never turned into numbers
    enriched = src.enrich(raws[0])
    assert src.normalize_job(enriched).description == "Full job description"


def test_naukri_job_link_must_be_on_naukri(tmp_path: Path) -> None:
    page = {"status": 200, "url": "https://www.naukri.com/s", "text": "",
            "items": [naukri_card(1, url="https://evil.example/job")]}  # fmt: skip
    src = source(NaukriSource, FakeBrowser([page]), tmp_path)
    [raw] = src.search_jobs(src.plan(query())[0], query())
    with pytest.raises(ValueError):
        src.normalize_job(raw)


def test_blocked_and_logged_out_pages_stop_the_source(tmp_path: Path) -> None:
    blocked = {
        "status": 403,
        "url": "https://www.naukri.com/s",
        "text": "Access Denied",
        "items": [],
    }
    src = source(NaukriSource, FakeBrowser([blocked]), tmp_path)
    with pytest.raises(SourceError, match="will not try to get around"):
        src.search_jobs(src.plan(query())[0], query())
    assert load_status(tmp_path)["naukri"]["state"] == "blocked"

    login = {
        "status": 200,
        "url": "https://www.linkedin.com/login?x",
        "text": "Sign in",
        "items": [],
    }
    src = source(LinkedInSource, FakeBrowser([login]), tmp_path)
    with pytest.raises(SourceAuthRequired):
        src.search_jobs(src.plan(query())[0], query())
    assert load_status(tmp_path)["linkedin"]["state"] == "expired"


def test_linkedin_cards_dates_and_pause_between_pages(tmp_path: Path) -> None:
    items = [
        {"id": "4475338717", "title": "Python Developer", "company": "HRM Counsel",
         "location": "Gurugram, Haryana, India (On-site)", "posted": "2026-10-02", "footer": ""},
        {"id": "4475338718", "title": "Backend Engineer", "company": "Acme",
         "location": "Noida", "posted": None, "footer": "Promoted\n3 days ago"},
        {"id": "4475338719", "title": None, "company": None, "location": None},  # not drawn
    ]  # fmt: skip
    pages = [
        {"status": 200, "url": "https://www.linkedin.com/jobs/search/", "text": "", "items": items},
        {"status": 200, "url": "https://www.linkedin.com/jobs/search/", "text": "", "items": []},
    ]
    browser = FakeBrowser(pages)
    src = source(LinkedInSource, browser, tmp_path, pages=2, max_searches=1)
    tasks = src.plan(query())
    raws = src.search_jobs(tasks[0], query())
    src.search_jobs(tasks[1], query())
    assert browser.render_all == [True, True]  # lazily drawn list: scrolled first
    assert "f_AL=true" in browser.opened[0] and "f_TPR=r172800" in browser.opened[0]
    assert "start=25" in browser.opened[1]
    assert src.sleeps == [10.0] or src.sleeps == [8.0]  # a pause between pages
    a, b = (src.normalize_job(r) for r in raws)
    assert a.url == "https://www.linkedin.com/jobs/view/4475338717/"
    assert a.posted_at_kind == "published_tz_unknown"
    assert b.posted_at_kind == "relative_upper_bound"
    assert len(raws) == 2


def test_details_are_capped_per_run(tmp_path: Path) -> None:
    page = {"status": 200, "url": "https://www.naukri.com/s", "text": "",
            "items": [naukri_card(i) for i in range(3)]}  # fmt: skip
    browser = FakeBrowser([page])
    src = source(NaukriSource, browser, tmp_path, max_details=1)
    raws = src.search_jobs(src.plan(query())[0], query())
    first, second = src.enrich(raws[0]), src.enrich(raws[1])
    assert first is not raws[0] and second is raws[1]  # only one job page opened


def test_freshness_and_experience_filters_are_asked_of_the_platforms(tmp_path: Path) -> None:
    empty = {"status": 200, "url": "https://x", "text": "", "items": []}
    nk = FakeBrowser([dict(empty)])
    src = source(NaukriSource, nk, tmp_path, experience_years=lambda: 1.0)
    src.search_jobs(src.plan(query())[0], query())
    assert nk.opened[0].endswith("?experience=1&jobAge=2")  # 48 h -> 2 days
    li = FakeBrowser([dict(empty)])
    src = source(LinkedInSource, li, tmp_path, experience_years=lambda: 1.0)
    src.search_jobs(src.plan(query())[0], query())
    assert "f_E=2%2C3" in li.opened[0] and "f_TPR=r172800" in li.opened[0]


def test_linkedin_badge_text_is_not_part_of_the_title(tmp_path: Path) -> None:
    item = {"id": "4475338717", "title": "AI Engineer with verification", "company": "Acme",
            "location": "Gurugram", "posted": "2026-10-03", "footer": "",
            "seen_at": NOW.isoformat()}  # fmt: skip
    src = source(LinkedInSource, FakeBrowser([]), tmp_path)
    from integrations.job_sources.base import RawJob

    assert src.normalize_job(RawJob(source_job_id=item["id"], payload=item)).title == "AI Engineer"
