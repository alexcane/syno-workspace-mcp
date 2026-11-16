"""Shared look for the server's own web pages (/setup today, settings or about later).

Each page package keeps its templates in `<package>/templates` and extends
`layout.html` from here. The theme is inlined in every page: the pages' CSP only
allows inline styles, and the NAS may have no Internet access for web fonts.
"""

from __future__ import annotations

from jinja2 import ChoiceLoader, Environment, PackageLoader, select_autoescape

from syno_workspace_mcp import __version__

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    # Not `no-referrer`: with it, browsers send `Origin: null` on form posts, which
    # a same-origin check must refuse.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_environment(*packages: str) -> Environment:
    """Templates of `packages` first, then the shared layout, theme and macros."""
    loaders = [PackageLoader(package, "templates") for package in packages]
    loaders.append(PackageLoader(__name__, "templates"))
    environment = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(default=True),
    )
    environment.globals["version"] = __version__
    return environment
