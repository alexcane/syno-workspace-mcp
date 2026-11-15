"""FastMCP server assembly: auth, health route, HTTP transport options."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from syno_workspace_mcp import __version__
from syno_workspace_mcp.auth import BearerTokenVerifier
from syno_workspace_mcp.config import Settings

logger = logging.getLogger(__name__)

SERVER_NAME = "syno-workspace-mcp"
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"


def create_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(
        name=SERVER_NAME,
        version=__version__,
        instructions="Tools for Synology Drive and Synology Spreadsheet on a DSM NAS.",
        auth=BearerTokenVerifier(settings.mcp_auth_token.get_secret_value()),
        mask_error_details=True,
    )

    @mcp.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        # Unauthenticated by design: status only, no DSM call, no details.
        return JSONResponse({"status": "ok"})

    return mcp


def http_options(settings: Settings) -> dict[str, Any]:
    """Transport options shared by `run_http_async` and `http_app` (tests)."""
    return {
        "path": MCP_PATH,
        "host_origin_protection": True,
        "allowed_hosts": list(settings.mcp_allowed_hosts),
    }


async def run_http(mcp: FastMCP, settings: Settings) -> None:
    if not settings.mcp_allowed_hosts and not settings.listens_on_loopback:
        logger.warning(
            "MCP_ALLOWED_HOSTS is empty: requests whose Host header is not loopback "
            "will be rejected with 421"
        )
    await mcp.run_http_async(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        show_banner=False,
        # Keep uvicorn on our root handler so its records go through redaction.
        uvicorn_config={"log_config": None},
        **http_options(settings),
    )
