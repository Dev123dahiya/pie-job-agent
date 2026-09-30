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
