"""Entry point: `python -m syno_workspace_mcp [--transport http|stdio]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from pydantic import ValidationError

from syno_workspace_mcp import __version__
from syno_workspace_mcp.config import Settings
from syno_workspace_mcp.logging import setup_logging
from syno_workspace_mcp.server import create_server, run_http

logger = logging.getLogger("syno_workspace_mcp")


def _format_validation_error(error: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for item in error.errors(include_input=False, include_url=False):
        field = ".".join(str(part) for part in item["loc"]).upper() or "<settings>"
        lines.append(f"  {field}: {item['msg']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syno-workspace-mcp")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="http (default, deployment target) or stdio (local development)",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings()  # values come from the environment
    except ValidationError as error:
        print(_format_validation_error(error), file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.secret_values())
    logger.info(
        "starting syno-workspace-mcp %s (transport=%s, modules=%s)",
        __version__,
        args.transport,
        ",".join(settings.syno_modules),
    )

    mcp = create_server(settings)
    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        asyncio.run(run_http(mcp, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
