"""Agent + draft HTTP API, with a scripted Claude injected via dependency override."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.api import deps
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.agent_runner import AgentLimits, AgentRunner
from database.models import Application
from tests.conftest import FakeClaude, decision


class Holder:
    claude: FakeClaude = FakeClaude()


@pytest.fixture
def agent(
    client: TestClient,
    session_factory: sessionmaker[Session],
    agent_settings: Settings,
    profile: UserProfile,
) -> Holder:
    holder = Holder()

    def runner() -> AgentRunner:
        return AgentRunner(
            session_factory,
            holder.claude,
            settings=agent_settings,
            profile_loader=lambda: profile,
            limits=AgentLimits(backoff_seconds=0),
            sleep=lambda s: None,
        )

    client.app.dependency_overrides[deps.agent_runner_dep] = runner  # type: ignore[attr-defined]
    return holder


def test_start_run_executes_in_background_and_persists(client: TestClient, agent: Holder) -> None:
    agent.claude = FakeClaude(
        decision(tool="get_application_statistics", summary="Count jobs"),
        decision(action="complete", message="0 applications so far."),
    )
    res = client.post(
        "/api/agent/runs",
        json={"task": "Summarize my pipeline", "mode": "read_only", "max_iterations": 5},
    )
    assert res.status_code == 202, res.text
    run_id = res.json()["id"]
    assert res.json()["run_type"] == "agent" and res.json()["mode"] == "read_only"
    detail = client.get(f"/api/agent/runs/{run_id}").json()  # the run survives the request
    assert detail["status"] == "COMPLETED" and detail["summary"] == "0 applications so far."
    assert detail["iterations"] == 2 and detail["max_iterations"] == 5
    actions = [(a["action"], a["tool"]) for a in detail["actions"]]
    assert ("TOOL_CALL_COMPLETED", "get_application_statistics") in actions
    assert client.get("/api/agent/runs", params={"run_type": "agent"}).json()[0]["id"] == run_id
    assert client.get("/api/agent/status").json()["state"] == "idle"


def test_validation_and_conflicts(client: TestClient, agent: Holder) -> None:
    assert client.post("/api/agent/runs", json={"task": "x"}).status_code == 422
    assert (
        client.post("/api/agent/runs", json={"task": "ok task", "mode": "external"}).status_code
        == 422
    )
    assert (
        client.post("/api/agent/runs", json={"task": "ok task", "max_iterations": 99}).status_code
        == 422
    )

    from backend.services.analysis import create_analysis_run
    from database.session import get_db

    db = next(client.app.dependency_overrides[get_db]())  # type: ignore[attr-defined]
    create_analysis_run(db, "busy", dev_mode=True)
    assert client.post("/api/agent/runs", json={"task": "Summarize"}).status_code == 409


def test_no_claude_backend_is_503(client: TestClient) -> None:
    # Tests run with ANALYZER_BACKEND=heuristic, which never falls back to a real Claude call.
    res = client.post("/api/agent/runs", json={"task": "Summarize my pipeline"})
    assert res.status_code == 503 and "needs Claude" in res.json()["detail"]


def test_needs_user_input_resume_and_cancel(client: TestClient, agent: Holder) -> None:
    agent.claude = FakeClaude(
        decision(
            action="need_user_input", summary="Ask", message="What is your work authorization?"
        )
    )
    run_id = client.post("/api/agent/runs", json={"task": "Prepare drafts"}).json()["id"]
    status = client.get("/api/agent/status").json()
    assert status["awaiting_user"]["id"] == run_id
    assert status["awaiting_user"]["pending_user_input"] == "What is your work authorization?"

    agent.claude = FakeClaude(decision(action="complete", message="Thanks."))
    res = client.post(f"/api/agent/runs/{run_id}/resume", json={"message": "Authorized in India"})
    assert res.status_code == 202
    assert client.get(f"/api/agent/runs/{run_id}").json()["status"] == "COMPLETED"
    assert client.post(f"/api/agent/runs/{run_id}/resume", json={}).status_code == 409  # completed

    agent.claude = FakeClaude(decision(action="need_user_input", message="?"))
    paused = client.post("/api/agent/runs", json={"task": "Another"}).json()["id"]
    assert client.post(f"/api/agent/runs/{paused}/cancel").json()["status"] == "STOPPED"
    assert client.post(f"/api/agent/runs/{uuid.uuid4()}/resume", json={}).status_code == 404


def test_tool_catalog_is_read_only_and_shows_disabled_tools(client: TestClient) -> None:
    tools = {t["name"]: t for t in client.get("/api/agent/tools").json()}
    assert tools["send_email"]["enabled"] is False
    assert tools["submit_application"]["permission"] == "EXTERNAL_ACTION"
    assert tools["get_job"]["enabled"] is True
    # There is no endpoint to execute arbitrary tools from the browser.
    assert client.post("/api/agent/tools/get_job", json={}).status_code in (404, 405)
    status = client.get("/api/agent/status").json()
    assert status["external_live_enabled"] is False
    assert status["default_external_mode"] == "external_dry_run"


def test_application_draft_endpoint(
    client: TestClient, shortlisted_job_id: Any, session_factory: sessionmaker[Session]
) -> None:
    res = client.post(f"/api/jobs/{shortlisted_job_id}/application-draft", json={"notes": "keen"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] and body["data"]["created"] is True
    assert body["data"]["job_status"] == "BLOCKED_NEEDS_USER"  # missing answers -> ask the user
    again = client.post(f"/api/jobs/{shortlisted_job_id}/application-draft").json()
    assert again["data"]["created"] is False
    detail = client.get(f"/api/jobs/{shortlisted_job_id}").json()
    assert detail["status"] == "BLOCKED_NEEDS_USER"
    assert "application_draft_created" in [e["event_type"] for e in detail["timeline"]]
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Application)) == 1
        app = s.scalar(select(Application))
        assert app is not None and app.applied_at is None and app.status.value == "DRAFT"


def test_draft_endpoint_errors(client: TestClient, seeded: None) -> None:
    jobs = client.get("/api/jobs", params={"q": "quantum"}).json()["items"]
    assert client.post(f"/api/jobs/{jobs[0]['id']}/application-draft").status_code == 409
    assert client.post(f"/api/jobs/{uuid.uuid4()}/application-draft").status_code == 404
