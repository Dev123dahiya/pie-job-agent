"""The API is reachable only from this computer (REQUIREMENTS 19.1)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import serve
from backend.core.local_only import host_name, is_local_client

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_local_requests_are_served(client: TestClient) -> None:
    for addr in ("127.0.0.1", "::1"):
        with TestClient(client.app, client=(addr, 50000)) as local:
            assert local.get("/api/health").status_code == 200, addr


@pytest.mark.parametrize("addr", ["192.168.1.23", "10.0.0.5", "172.16.0.9", "8.8.8.8"])
def test_requests_from_other_devices_are_refused(client: TestClient, addr: str) -> None:
    with TestClient(client.app, client=(addr, 50000)) as remote:
        for method, path in [
            ("get", "/api/health"),
            ("get", "/api/jobs"),
            ("post", "/api/agent/runs"),
        ]:
            res = getattr(remote, method)(path)
            assert res.status_code == 403, (addr, path)
            assert "this computer" in res.json()["detail"]


def test_non_local_host_header_is_refused(client: TestClient) -> None:
    # DNS rebinding: a page on evil.example resolves to 127.0.0.1 but keeps its own Host.
    with TestClient(client.app, base_url="http://evil.example") as c:
        assert c.get("/api/health").status_code == 403
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
        assert client.get("/api/health", headers={"host": host}).status_code == 200, host


def test_helpers() -> None:
    assert is_local_client("127.0.0.1") and is_local_client("::1")
    assert not is_local_client("192.168.1.23") and not is_local_client(None)
    assert not is_local_client("0.0.0.0")
    assert host_name("localhost:8000") == "localhost"
    assert host_name("[::1]:8000") == "::1"


def test_serve_always_binds_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    serve.main(["--port", "8123", "--reload"])
    assert seen == {"app": "backend.main:app", "host": "127.0.0.1", "port": 8123, "reload": True}
    with pytest.raises(SystemExit):  # there is no option to change the address
        serve.main(["--host", "0.0.0.0"])


def test_dashboard_scripts_bind_to_localhost() -> None:
    scripts = json.loads((REPO_ROOT / "dashboard" / "package.json").read_text("utf-8"))["scripts"]
    for name in ("dev", "start"):
        assert "-H 127.0.0.1" in scripts[name], name
        assert "0.0.0.0" not in scripts[name], name
