"""Jobs and agent API, end to end on the mock source with the heuristic analyzer."""

import uuid

import pytest
from fastapi.testclient import TestClient

from agent.analysis.analyzers import AnalyzerUnavailableError
from backend.api import deps


@pytest.fixture
def discovered(client: TestClient) -> str:
    res = client.post("/api/jobs/discover", json={"source": "mock"})
    assert res.status_code == 202, res.text
    return res.json()["run_ids"][0]  # TestClient runs background tasks before returning


def titles(res) -> list[str]:  # type: ignore[no-untyped-def]
    return [j["title"] for j in res.json()["items"]]


def test_discover_endpoint_records_a_run(client: TestClient, discovered: str) -> None:
    run = client.get(f"/api/agent/runs/{discovered}").json()
    assert (
        run["status"] == "COMPLETED" and run["run_type"] == "discovery" and run["source"] == "mock"
    )
    assert (run["jobs_found"], run["jobs_new"], run["jobs_duplicate"], run["jobs_failed"]) == (
        9,
        7,
        2,
        1,
    )
    assert "Software Engineer" in run["search_terms"] and "Gurugram" in run["locations"]
    assert run["actions"][0]["action"] == "DISCOVERY_STARTED"
    status = client.get("/api/agent/status").json()
    assert status["state"] == "idle" and status["recent_runs"][0]["id"] == discovered
    assert status["analyzer_detail"] == "Heuristic analyzer (not Claude)"


def test_list_and_filters(client: TestClient, discovered: str) -> None:
    body = client.get("/api/jobs").json()
    assert body["total"] == 7
    job = body["items"][0]
    assert set(job) >= {
        "company",
        "title",
        "location",
        "salary_min",
        "posted_at",
        "source",
        "freshness",
        "status",
        "analysis",
    }
    assert "raw" not in job and "fingerprint" not in job  # internals are not exposed

    assert client.get("/api/jobs", params={"freshness": "unknown"}).json()["total"] == 1
    assert titles(client.get("/api/jobs", params={"freshness": "stale"})) == ["Software Developer"]
    assert client.get("/api/jobs", params={"location": "noida"}).json()["total"] == 2
    assert titles(client.get("/api/jobs", params={"q": "nimbus"})) == ["Backend Software Engineer"]
    assert client.get("/api/jobs", params={"source": "greenhouse"}).json()["total"] == 0
    # Minimum salary keeps jobs with no published salary unless disclosed-only is requested.
    assert client.get("/api/jobs", params={"min_salary": 2_000_000}).json()["total"] == 5
    assert titles(
        client.get("/api/jobs", params={"min_salary": 2_000_000, "salary_known_only": True})
    ) == ["Senior Staff ML Engineer"]
    assert titles(client.get("/api/jobs", params={"sort": "salary", "limit": 1})) == [
        "Senior Staff ML Engineer"
    ]
    assert client.get("/api/jobs", params={"freshness": "ancient"}).status_code == 422

    options = client.get("/api/jobs/filters").json()
    assert options["sources"] == ["mock"] and options["freshness_window_hours"] == 48


def test_detail_analyze_and_shortlist(client: TestClient, discovered: str) -> None:
    job_id = next(
        j["id"]
        for j in client.get("/api/jobs").json()["items"]
        if j["title"] == "Backend Software Engineer"
    )
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "DISCOVERED" and detail["analysis"] is None
    assert detail["timeline"][0]["event_type"] == "job_discovered"

    analyzed = client.post(f"/api/jobs/{job_id}/analyze")
    assert analyzed.status_code == 200, analyzed.text
    body = analyzed.json()
    assert body["status"] == "SHORTLISTED"
    a = body["analysis"]
    assert a["analyzer"] == "heuristic" and a["role_match"] == "strong" and a["shortlisted"] is True
    assert {"role_match", "freshness", "analyzer_recommendation"} <= {
        r["rule"] for r in a["rule_results"]
    }
    assert [e["event_type"] for e in body["timeline"]] == [
        "job_discovered",
        "job_analyzed",
        "job_shortlisted",
    ]

    shortlisted = client.get("/api/jobs/shortlisted").json()
    assert titles_from(shortlisted) == ["Backend Software Engineer"]
    assert client.get("/api/jobs", params={"shortlisted": True}).json()["total"] == 1


def titles_from(body: dict) -> list[str]:
    return [j["title"] for j in body["items"]]


def test_analyze_shortlist_batch(client: TestClient, discovered: str) -> None:
    res = client.post("/api/jobs/analyze-shortlist", json={"limit": 50})
    assert res.status_code == 202
    run = client.get(f"/api/agent/runs/{res.json()['run_ids'][0]}").json()
    assert run["run_type"] == "analysis" and run["status"] == "COMPLETED"
    # 3 shortlisted: the undated (UNKNOWN freshness) job is rejected like a stale one.
    assert (run["jobs_analyzed"], run["jobs_shortlisted"], run["jobs_rejected"]) == (6, 3, 3)
    assert client.get("/api/jobs/shortlisted").json()["total"] == 3
    assert (
        client.get("/api/jobs", params={"sort": "match"}).json()["items"][0]["analysis"] is not None
    )
    # Nothing left to analyze (the stale job is skipped by default).
    assert client.post("/api/jobs/analyze-shortlist").json()["run_ids"] == []


def test_errors(client: TestClient) -> None:
    assert client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404
    assert client.post(f"/api/jobs/{uuid.uuid4()}/analyze").status_code == 404
    assert client.get("/api/jobs/not-a-uuid").status_code == 422
    assert client.post("/api/jobs/discover", json={"source": "nope"}).status_code == 400
    assert client.post(f"/api/agent/runs/{uuid.uuid4()}/cancel").status_code == 404


def test_conflicting_runs_are_refused(client: TestClient, discovered: str) -> None:
    from backend.services.analysis import create_analysis_run
    from database.session import get_db

    db = next(client.app.dependency_overrides[get_db]())  # type: ignore[attr-defined]
    active = create_analysis_run(db, "busy", dev_mode=True)
    assert client.post("/api/jobs/discover", json={"source": "mock"}).status_code == 409
    assert client.post("/api/jobs/analyze-shortlist").status_code == 409
    assert client.get("/api/agent/status").json()["state"] == "running"
    cancelled = client.post(f"/api/agent/runs/{active.id}/cancel").json()
    assert cancelled["status"] == "STOPPING"
    assert client.get("/api/agent/status").json()["state"] == "stopping"


def test_analyzer_unavailable_is_503(client: TestClient, discovered: str) -> None:
    def unavailable():  # type: ignore[no-untyped-def]
        from fastapi import HTTPException

        raise HTTPException(503, str(AnalyzerUnavailableError("No Claude analyzer available")))

    client.app.dependency_overrides[deps.analyzer_dep] = unavailable  # type: ignore[attr-defined]
    job_id = client.get("/api/jobs").json()["items"][0]["id"]
    res = client.post(f"/api/jobs/{job_id}/analyze")
    assert res.status_code == 503 and "No Claude analyzer" in res.json()["detail"]


# --- D2: six-status, platform, score, date, company and apply-type filters; user actions ---


def test_jobs_carry_the_six_statuses_platform_and_apply_type(
    client: TestClient, discovered: str
) -> None:
    items = client.get("/api/jobs").json()["items"]
    assert items and all(j["user_status"] == "found" for j in items)
    assert {j["platform"] for j in items} == {"company_site"}
    assert {j["apply_type"] for j in items} == {"company_form"}  # mock = an employer form


def test_new_filters(client: TestClient, discovered: str) -> None:
    client.post("/api/jobs/analyze-shortlist", json={"limit": 50})
    total = client.get("/api/jobs").json()["total"]

    def count(**params: object) -> int:
        res = client.get("/api/jobs", params=params)
        assert res.status_code == 200, res.text
        return res.json()["total"]

    assert count(status_group="found") + count(status_group="skipped") == total
    assert count(status_group=["found", "skipped"]) == total
    assert count(platform="company_site") == total and count(platform="naukri") == 0
    assert count(apply_type="company_form") == total and count(apply_type="external_link") == 0
    scored = client.get("/api/jobs", params={"min_score": 0}).json()["items"]
    assert scored and all(j["analysis"]["match_score"] is not None for j in scored)
    top = max(j["analysis"]["match_score"] for j in scored)
    assert count(min_score=top) >= 1 and count(min_score=top, max_score=top) == count(min_score=top)
    company = scored[0]["company"]
    assert count(company=company.upper()[:4]) >= 1
    assert count(found_from="2000-01-01") == total and count(found_to="2000-01-01") == 0
    # Invalid input is refused, not silently ignored.
    assert client.get("/api/jobs", params={"min_score": 80, "max_score": 10}).status_code == 422
    assert client.get("/api/jobs", params={"min_score": 101}).status_code == 422
    assert client.get("/api/jobs", params={"platform": "indeed"}).status_code == 422
    assert client.get("/api/jobs", params={"status_group": "maybe"}).status_code == 422


def test_skip_and_move_to_shortlist(client: TestClient, discovered: str) -> None:
    client.post("/api/jobs/analyze-shortlist", json={"limit": 50})
    jobs = client.get("/api/jobs", params={"min_score": 0, "sort": "match"}).json()["items"]
    shortlisted = next(j for j in jobs if j["status"] == "SHORTLISTED")

    skipped = client.post(f"/api/jobs/{shortlisted['id']}/skip", json={"reason": "Not for me"})
    assert skipped.status_code == 200 and skipped.json()["status"] == "REJECTED_BY_AGENT"
    detail = client.get(f"/api/jobs/{shortlisted['id']}").json()
    assert detail["user_status"] == "skipped"
    last = detail["timeline"][-1]
    assert last["event_type"] == "skipped_by_user" and last["reason"] == "Not for me"
    # Skipping twice is refused; shortlisting it again works (score is high enough).
    assert client.post(f"/api/jobs/{shortlisted['id']}/skip").status_code == 409
    back = client.post(f"/api/jobs/{shortlisted['id']}/shortlist")
    assert back.status_code == 200 and back.json()["status"] == "SHORTLISTED"

    # The dashboard can never shortlist below the minimum match score (70).
    low = [
        j for j in jobs if j["analysis"]["match_score"] < 70 and j["status"] == "REJECTED_BY_AGENT"
    ]
    assert low, "the mock data must include a low-scoring job for this check"
    for job in low:
        res = client.post(f"/api/jobs/{job['id']}/shortlist")
        assert res.status_code == 409 and "below the minimum" in res.json()["detail"]
    # Unanalyzed jobs have no score to check.
    fresh = client.get("/api/jobs", params={"status": "DISCOVERED"}).json()["items"]
    for job in fresh[:1]:
        res = client.post(f"/api/jobs/{job['id']}/shortlist")
        assert res.status_code == 409
    missing = client.post("/api/jobs/00000000-0000-0000-0000-000000000000/skip")
    assert missing.status_code == 404


# --- C9: Manual apply list ------------------------------------------------------------------


def test_manual_apply_list(client: TestClient, discovered: str) -> None:
    client.post("/api/jobs/analyze-shortlist", json={"limit": 50})
    shortlisted = client.get("/api/jobs/shortlisted").json()["items"]
    a, b = shortlisted[0]["id"], shortlisted[1]["id"]
    discovered_only = client.get("/api/jobs", params={"status": "DISCOVERED"}).json()["items"]

    for job_id in (a, b):
        res = client.post(f"/api/jobs/{job_id}/manual")
        assert res.status_code == 200 and res.json()["status"] == "MANUAL_APPLY"
    listed = client.get("/api/jobs/manual").json()
    assert listed["total"] == 2
    item = next(i for i in listed["items"] if i["job_id"] == a)
    assert item["url"].startswith("http") and item["match_score"] is not None
    assert item["reason"] == "You chose to apply yourself"
    detail = client.get(f"/api/jobs/{a}").json()
    assert detail["user_status"] == "manual"
    assert client.get("/api/dashboard/home").json()["quick_links"]["manual"] == 2

    applied = client.post(f"/api/jobs/{a}/manual/applied")
    assert applied.status_code == 200 and applied.json()["status"] == "SUBMITTED"
    detail = client.get(f"/api/jobs/{a}").json()
    assert detail["user_status"] == "applied" and detail["application"]["status"] == "APPLIED"
    assert detail["timeline"][-1]["event_type"] == "applied_manually_by_user"
    # Not PIE's submission: today's daily limits are untouched.
    limits = {r["platform"]: r["used"] for r in client.get("/api/dashboard/limits").json()}
    assert limits["total"] == 0

    skipped = client.post(f"/api/jobs/{b}/manual/skip")
    assert skipped.status_code == 200 and skipped.json()["status"] == "REJECTED_BY_AGENT"
    assert client.get("/api/jobs/manual").json()["total"] == 0
    # Only manual-list jobs can be marked applied; only eligible jobs can be moved there.
    assert client.post(f"/api/jobs/{b}/manual/applied").status_code == 409
    if discovered_only:
        assert client.post(f"/api/jobs/{discovered_only[0]['id']}/manual").status_code == 409
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/api/jobs/{missing}/manual").status_code == 404
