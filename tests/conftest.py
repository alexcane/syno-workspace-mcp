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
