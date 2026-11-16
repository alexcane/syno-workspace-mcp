"""Run the /setup page until the configuration is saved, then stop the process."""

from __future__ import annotations

import logging

import uvicorn

from syno_workspace_mcp.config import BootSettings
from syno_workspace_mcp.setup.app import SETUP_PATH, create_setup_app, generate_code

logger = logging.getLogger("syno_workspace_mcp")


def announce(code: str, settings: BootSettings, reason: str) -> None:
    # The code is meant to be read in `docker logs`: it is not a secret to redact.
    # Container Manager's log view cuts long lines: the code gets a short line of its own.
    logger.warning(
        "setup mode (%s): open https://<your NAS host>%s through the DSM reverse proxy "
        "to configure the server (local port %s)",
        reason,
        SETUP_PATH,
        settings.mcp_port,
    )
    logger.warning("Setup code: %s", code)


async def run_setup(settings: BootSettings, reason: str) -> None:
    code = generate_code()
    server: uvicorn.Server | None = None

    async def stop() -> None:
        # Docker's restart policy starts the server again with the saved configuration.
        if server is not None:
            server.should_exit = True

    app = create_setup_app(settings.data_dir, code=code, on_finished=stop)
    config = uvicorn.Config(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_config=None,
        proxy_headers=False,
        server_header=False,
    )
    server = uvicorn.Server(config)
    announce(code, settings, reason)
    await server.serve()
