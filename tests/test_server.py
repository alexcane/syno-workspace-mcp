from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from starlette.testclient import TestClient

from syno_workspace_mcp.auth import BearerTokenVerifier

from .conftest import PUBLIC_HOST, TEST_TOKEN

INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def bearer(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def test_health_is_public(http_client: TestClient) -> None:
    response = http_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_without_token_is_401(http_client: TestClient) -> None:
    response = http_client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401


def test_mcp_with_wrong_token_is_401(http_client: TestClient) -> None:
    response = http_client.post("/mcp", json=INITIALIZE, headers=bearer("x" * 64))

    assert response.status_code == 401


def test_mcp_with_valid_token_initializes(http_client: TestClient) -> None:
    response = http_client.post("/mcp", json=INITIALIZE, headers=bearer(TEST_TOKEN))

    assert response.status_code == 200
    assert "syno-workspace-mcp" in response.text


def test_unknown_host_is_rejected(http_client: TestClient) -> None:
    response = http_client.get("/health", headers={"Host": "evil.example.test"})

    assert response.status_code == 421


def test_allowed_host_with_port_is_accepted(http_client: TestClient) -> None:
    response = http_client.get("/health", headers={"Host": f"{PUBLIC_HOST}:3443"})

    assert response.status_code == 200


def test_foreign_origin_is_rejected(http_client: TestClient) -> None:
    response = http_client.post(
        "/mcp",
        json=INITIALIZE,
        headers={**bearer(TEST_TOKEN), "Origin": "https://evil.example.test"},
    )

    assert response.status_code == 403


async def test_in_memory_client_sees_server(mcp: FastMCP) -> None:
    # The in-memory client negotiates the modern protocol (`server/discover`), so
    # use the era-neutral accessors rather than `initialize_result` or `ping`.
    async with Client(mcp) as client:
        assert client.server_info is not None
        assert client.server_info.name == "syno-workspace-mcp"
        assert client.instructions is not None


async def test_verifier_accepts_only_exact_token() -> None:
    verifier = BearerTokenVerifier(TEST_TOKEN)

    accepted = await verifier.verify_token(TEST_TOKEN)
    assert accepted is not None
    assert accepted.token == TEST_TOKEN
    assert await verifier.verify_token(TEST_TOKEN[:-1]) is None
    assert await verifier.verify_token(TEST_TOKEN + "x") is None
    assert await verifier.verify_token("") is None


def test_verifier_rejects_empty_expected_token() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        BearerTokenVerifier("")
