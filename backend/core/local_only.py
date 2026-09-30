"""Keep the API on this laptop.

PIE has no login, so it must never be reachable from another device. Two layers:

1. `python -m backend.serve` binds the server to 127.0.0.1 only (see backend/serve.py).
2. `LocalOnlyMiddleware` refuses any request that does not come from this machine, even if
   the server was started some other way (for example `uvicorn --host 0.0.0.0`). It also
   refuses requests whose Host header is not a local name, so a web page on the internet
   cannot reach the API through DNS rebinding.
"""

import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

LOCAL_HOST = "127.0.0.1"
# Host header names that mean "this machine". "testserver" is what FastAPI's TestClient sends.
ALLOWED_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
# FastAPI's TestClient reports its client as "testclient" (never a network address).
_TEST_CLIENT = "testclient"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def is_local_client(host: str | None) -> bool:
    if host == _TEST_CLIENT:
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def host_name(header: str) -> str:
    """'localhost:8000' -> 'localhost'; '[::1]:8000' -> '::1'."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1 : header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class LocalOnlyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = (scope.get("client") or (None, None))[0]
        headers = dict(scope.get("headers") or [])
        host = host_name(headers.get(b"host", b"").decode("latin-1"))
        if not is_local_client(client):
            reason = "PIE only accepts requests from this computer"
        elif host not in ALLOWED_HOST_NAMES:
            reason = "PIE only accepts requests addressed to localhost"
        else:
            await self.app(scope, receive, send)
            return
        logger.warning("Refused a non-local request (client=%s, host=%s)", client, host)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": reason}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
