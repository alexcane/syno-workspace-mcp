"""Synology error types and their translation into readable MCP tool errors."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

# Codes shared by every DSM Web API.
_COMMON_MESSAGES: dict[int, str] = {
    100: "unknown DSM error",
    101: "invalid parameter",
    102: "the requested API does not exist (is the package installed on the NAS?)",
    103: "the requested API method does not exist",
    104: "the requested API version is not supported by this DSM",
    105: "permission denied: the DSM account is not allowed to perform this operation",
    106: "DSM session timed out",
    107: "DSM session interrupted by a duplicate login",
    108: "DSM session interrupted",
    119: "DSM session id not found",
}

# Codes specific to SYNO.API.Auth (login).
_AUTH_MESSAGES: dict[int, str] = {
    400: "DSM login failed: account does not exist or password is wrong "
    "(check SYNO_USERNAME / SYNO_PASSWORD)",
    401: "DSM login failed: the account is disabled",
    403: "DSM login failed: the account requires a 2FA code; use a dedicated account without 2FA",
    404: "DSM login failed: 2FA code rejected; use a dedicated account without 2FA",
    407: "DSM login failed: this IP address is blocked by DSM auto-block "
    "(Control Panel > Security > Protection)",
}

# Codes returned by SYNO.SynologyDrive.* as mapped by the upstream project.
_DRIVE_MESSAGES: dict[int, str] = {
    408: "file or folder not found",
    414: "no write permission on this file or folder",
    417: "a file or folder with this name already exists",
    418: "disk quota exceeded",
}

# 106 and 119 per the brief; 108 is also treated as expired by the upstream project.
SESSION_EXPIRED_CODES = frozenset({106, 108, 119})


class SynologyError(Exception):
    """Base class for every error raised by the Synology clients."""

    def __init__(self, message: str, *, code: int | None = None, api: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.api = api

    def __str__(self) -> str:
        if self.code is None:
            return self.message
        return f"{self.message} (DSM code {self.code})"


class DsmNetworkError(SynologyError):
    """DSM could not be reached or answered with something that is not a DSM envelope."""


class DsmAuthError(SynologyError):
    """Login to DSM failed."""


class DsmApiError(SynologyError):
    """A DSM API call returned `success: false`."""


class InvalidInputError(SynologyError):
    """Input rejected locally, before any call to DSM."""


def message_for_code(code: int, api: str) -> str:
    if api == "SYNO.API.Auth" and code in _AUTH_MESSAGES:
        return _AUTH_MESSAGES[code]
    if api.startswith("SYNO.SynologyDrive.") and code in _DRIVE_MESSAGES:
        return _DRIVE_MESSAGES[code]
    if code in _COMMON_MESSAGES:
        return _COMMON_MESSAGES[code]
    return f"{api} returned error code {code}"


def to_tool_error(error: SynologyError) -> ToolError:
    """ToolError messages reach the client even with `mask_error_details=True`."""
    return ToolError(str(error))
