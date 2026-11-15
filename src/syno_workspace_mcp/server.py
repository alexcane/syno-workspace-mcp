"""FastMCP server assembly: auth, health route, HTTP transport options."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from syno_workspace_mcp import __version__
from syno_workspace_mcp.auth import BearerTokenVerifier
from syno_workspace_mcp.clients.drive import DriveClient
from syno_workspace_mcp.clients.dsm import DsmClient
from syno_workspace_mcp.clients.spreadsheet import SpreadsheetClient
from syno_workspace_mcp.config import Settings
from syno_workspace_mcp.store import SpreadsheetRegistry
from syno_workspace_mcp.tools.drive import DRIVE_CLIENT_KEY, register_drive_tools
from syno_workspace_mcp.tools.spreadsheet import (
    REGISTRY_KEY,
    SPREADSHEET_CLIENT_KEY,
    register_spreadsheet_tools,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "syno-workspace-mcp"
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"


def create_dsm_client(settings: Settings) -> DsmClient:
    return DsmClient(
        str(settings.syno_dsm_url),
        settings.syno_username,
        settings.syno_password,
        verify_tls=settings.syno_verify_tls,
    )


def create_spreadsheet_client(settings: Settings) -> SpreadsheetClient:
    return SpreadsheetClient(
        str(settings.syno_ss_api_url),
        str(settings.syno_ss_dsm_url or settings.syno_dsm_url),
        settings.syno_username,
        settings.syno_password,
        verify_tls=settings.syno_verify_tls,
    )


def create_server(settings: Settings) -> FastMCP:
    @lifespan
    async def clients_lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
        # One DSM session and one Spreadsheet API token for the whole process,
        # both closed (logout / revoke) at shutdown.
        async with AsyncExitStack() as stack:
            dsm = await stack.enter_async_context(create_dsm_client(settings))
            # spreadsheet_list lists .osheet files through Drive, so Spreadsheet needs it too.
            context: dict[str, Any] = {DRIVE_CLIENT_KEY: DriveClient(dsm)}
            if "spreadsheet" in settings.syno_modules:
                context[SPREADSHEET_CLIENT_KEY] = await stack.enter_async_context(
                    create_spreadsheet_client(settings)
                )
                context[REGISTRY_KEY] = SpreadsheetRegistry(settings.data_dir)
            yield context

    mcp = FastMCP(
        name=SERVER_NAME,
        version=__version__,
        instructions="Tools for Synology Drive and Synology Spreadsheet on a DSM NAS.",
        auth=BearerTokenVerifier(settings.mcp_auth_token.get_secret_value()),
        mask_error_details=True,
        lifespan=clients_lifespan,
    )

    if "drive" in settings.syno_modules:
        register_drive_tools(mcp)
    if "spreadsheet" in settings.syno_modules:
        register_spreadsheet_tools(mcp)

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
