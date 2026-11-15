"""Static bearer token verification with a constant-time comparison."""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier

CLIENT_ID = "syno-workspace-mcp-client"


class BearerTokenVerifier(TokenVerifier):
    """Accept exactly one shared bearer token.

    FastMCP's `StaticTokenVerifier` is documented as development-only and uses a
    dict lookup; this verifier compares with `hmac.compare_digest` instead.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("bearer token must not be empty")
        super().__init__()
        self._expected = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode(), self._expected):
            return None
        return AccessToken(token=token, client_id=CLIENT_ID, scopes=[])
