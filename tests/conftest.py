from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
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
