from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import respx
from fastmcp import FastMCP
from starlette.testclient import TestClient

from syno_workspace_mcp.config import Settings
from syno_workspace_mcp.server import create_server, http_options

TEST_TOKEN = "t" * 64
TEST_PASSWORD = "dsm-password-for-tests"
PUBLIC_HOST = "drive.example.test"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Minimal valid environment; tests override single variables as needed."""
    values = {
        "SYNO_DSM_URL": "https://nas.example.test:5001",
        "SYNO_USERNAME": "mcp_service",
        "SYNO_PASSWORD": TEST_PASSWORD,
        "MCP_AUTH_TOKEN": TEST_TOKEN,
        "MCP_ALLOWED_HOSTS": PUBLIC_HOST,
        "DATA_DIR": str(tmp_path),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return Settings()


@pytest.fixture
def mcp(settings: Settings) -> FastMCP:
    return create_server(settings)


@pytest.fixture
def http_client(mcp: FastMCP, settings: Settings) -> Iterator[TestClient]:
    app = mcp.http_app(**http_options(settings))
    # Loopback base URL: the Host/Origin guard always accepts loopback hosts.
    with TestClient(app, base_url="http://localhost") as client:
        yield client


# DSM mocks -------------------------------------------------------------------

DSM_URL = "https://nas.example.test:5001"
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


class FakeDsm:
    """respx-backed DSM: `auth.cgi` login/logout plus one `entry.cgi` route."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.login_responses: list[dict[str, Any]] = [load_fixture("auth_login.json")]
        self.logins: list[dict[str, str]] = []
        self.logouts: list[dict[str, str]] = []
        self.auth = router.post(f"{DSM_URL}/webapi/auth.cgi").mock(side_effect=self._auth)
        self.entry = router.route(url__startswith=f"{DSM_URL}/webapi/entry.cgi")

    def _auth(self, request: httpx.Request) -> httpx.Response:
        body = form(request)
        if body.get("method") == "logout":
            self.logouts.append(body)
            return httpx.Response(200, json={"success": True})
        self.logins.append(body)
        index = min(len(self.logins), len(self.login_responses)) - 1
        return httpx.Response(200, json=self.login_responses[index])

    def entry_params(self, call_index: int = -1) -> dict[str, str]:
        return dict(self.entry.calls[call_index].request.url.params)


@pytest.fixture
def fake_dsm() -> Iterator[FakeDsm]:
    with respx.mock(assert_all_called=False) as router:
        yield FakeDsm(router)


# Spreadsheet API container mock -------------------------------------------------

SS_URL = "http://127.0.0.1:3000"
SS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzaWQiOiJ0ZXN0In0.signature-for-tests"


def json_body(request: httpx.Request) -> Any:
    return json.loads(request.content.decode()) if request.content else None


class FakeSpreadsheetApi:
    """respx-backed `synology/spreadsheet-api`: authorize, revoke and scripted routes.

    Routes are keyed by method and raw (still percent-encoded) path, so tests see
    exactly what goes on the wire. An unscripted request answers 500.
    """

    def __init__(self, router: respx.MockRouter) -> None:
        self.authorize_responses: list[httpx.Response] = [
            httpx.Response(200, json={"token": SS_TOKEN})
        ]
        self.authorizations: list[dict[str, Any]] = []
        self.revocations: list[httpx.Request] = []
        self.calls: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], list[httpx.Response]] = {}
        router.route(url__startswith=SS_URL).mock(side_effect=self._handle)

    def on(self, method: str, raw_path: str, *responses: httpx.Response) -> None:
        """Answer `responses` in order; the last one repeats."""
        self._routes[(method, raw_path)] = list(responses)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        if path == "/spreadsheets/authorize":
            self.authorizations.append(json_body(request))
            index = min(len(self.authorizations), len(self.authorize_responses)) - 1
            return self.authorize_responses[index]
        if path == "/spreadsheets/authorize/token/revoke":
            self.revocations.append(request)
            return httpx.Response(200, json={"success": True})
        self.calls.append(request)
        queue = self._routes.get((request.method, path))
        if not queue:
            return httpx.Response(500, json={"error": f"unscripted {request.method} {path}"})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def call(self, index: int = -1) -> httpx.Request:
        return self.calls[index]


@pytest.fixture
def fake_ss(fake_dsm: FakeDsm) -> FakeSpreadsheetApi:
    return FakeSpreadsheetApi(fake_dsm.router)
