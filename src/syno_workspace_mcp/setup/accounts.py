"""DSM calls made by the /setup page: test the account, prepare its access.

DSM 7.4 refuses `SYNO.Core.User create` (code 105) to any session opened through the
Web API, even after `SYNO.Core.User.PasswordConfirm` (verified on a real NAS): only
its web interface may create accounts. The account is therefore created by hand in
DSM; with administrator credentials, the page then does the rest, using the calls
DSM's own UI sends (captured on the NAS): optionally `SYNO.Core.Share create` and
`SYNO.SynologyDrive.Share set` to add a Drive team folder, then
`SYNO.Core.Share.Permission set_by_user_group` and `SYNO.Core.AppPriv.Rule set`.
Every step can be replayed safely. Administrator credentials live only for the
duration of one request and the admin session is always logged out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from syno_workspace_mcp.clients.dsm import DsmClient
from syno_workspace_mcp.errors import DsmApiError, DsmAuthError, SynologyError

DRIVE_APP_ID = "SYNO.SDS.Drive.Application"
USER_NOT_FOUND = 3106
FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")

_LOGIN_MESSAGES = {
    400: "wrong account name or password",
    401: "this account is disabled",
    403: "this account uses two-factor authentication: enter its current 6-digit code",
    404: "the two-factor code was rejected (codes expire after 30 seconds)",
    407: "DSM blocked this address after too many failed logins "
    "(Control Panel > Security > Protection)",
}


class SetupError(Exception):
    """A failure the page shows as is."""


@dataclass
class PreparationReport:
    """What `prepare_service_account` did, shown when it stops half-way."""

    username: str
    new_folder: str | None = None
    folder_created: bool = False
    folder_enabled: bool = False
    folders_granted: bool = False
    drive_allowed: bool = False
    account_checked: bool = False
    error: str | None = None
    available_folders: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.account_checked and self.error is None


def login_message(error: DsmAuthError) -> str:
    if error.code in _LOGIN_MESSAGES:
        return _LOGIN_MESSAGES[error.code]
    return str(error)


async def check_account(dsm_url: str, verify_tls: bool, username: str, password: SecretStr) -> None:
    """Log in with the account the server will use; raise `SetupError` if DSM refuses."""
    try:
        async with DsmClient(dsm_url, username, password, verify_tls=verify_tls) as client:
            await client.login()
    except DsmAuthError as error:
        raise SetupError(f"DSM login failed for {username}: {login_message(error)}") from error
    except SynologyError as error:
        raise SetupError(f"cannot reach DSM at {dsm_url}: {error}") from error


def _json(value: Any) -> str:
    # Core APIs read each parameter as JSON (that is what the DSM UI sends).
    return json.dumps(value)


async def _user_exists(client: DsmClient, username: str) -> bool:
    try:
        await client.call(
            "SYNO.Core.User", "get", 1, {"action": _json("get"), "name": _json(username)}
        )
    except DsmApiError as error:
        if error.code == USER_NOT_FOUND:
            return False
        raise
    return True


async def _drive_folders(client: DsmClient) -> dict[str, bool]:
    """Shared folders known to Drive, mapped to whether they are enabled as team folders."""
    data = await client.call(
        "SYNO.SynologyDrive.Share",
        "list",
        1,
        {
            "action": _json("list"),
            "offset": 0,
            "limit": 1000,
            "sort_by": _json("share_name"),
            "sort_direction": _json("ASC"),
        },
    )
    items = data.get("items")
    if not isinstance(items, list):
        return {}
    return {
        str(item["share_name"]): item.get("share_enable") is True
        for item in items
        if isinstance(item, dict) and "share_name" in item
    }


async def _shared_folders(client: DsmClient) -> set[str]:
    data = await client.call(
        "SYNO.Core.Share", "list", 1, {"shareType": _json("all"), "additional": _json([])}
    )
    shares = data.get("shares")
    if not isinstance(shares, list):
        return set()
    return {str(share["name"]) for share in shares if isinstance(share, dict) and "name" in share}


async def _default_volume(client: DsmClient) -> str:
    data = await client.call(
        "SYNO.Core.Storage.Volume",
        "list",
        1,
        {"limit": -1, "offset": 0, "location": _json("internal")},
    )
    volumes = data.get("volumes")
    for volume in volumes if isinstance(volumes, list) else []:
        if (
            isinstance(volume, dict)
            and volume.get("status") == "normal"
            and not volume.get("readonly")
            and isinstance(volume.get("volume_path"), str)
        ):
            return str(volume["volume_path"])
    raise SetupError("no writable volume found to create the shared folder on")


def parse_folder_name(raw: str) -> str:
    value = raw.strip()
    if not FOLDER_NAME.fullmatch(value):
        raise SetupError(
            "invalid folder name: up to 32 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


async def prepare_service_account(
    *,
    dsm_url: str,
    verify_tls: bool,
    admin_username: str,
    admin_password: SecretStr,
    otp_code: str | None,
    username: str,
    password: SecretStr,
    team_folders: list[str],
    new_folder: str | None = None,
) -> PreparationReport:
    """Give `username` read/write on its team folders and Synology Drive, then test it.

    `new_folder`, when given, is first created as a shared folder (if missing) and
    enabled as a Drive team folder; it is added to `team_folders`.
    """
    folders = list(dict.fromkeys([*([new_folder] if new_folder else []), *team_folders]))
    report = PreparationReport(username=username, new_folder=new_folder)
    client = DsmClient(
        dsm_url, admin_username, admin_password, verify_tls=verify_tls, otp_code=otp_code
    )
    step = "administrator login"
    try:
        try:
            await client.login()
        except DsmAuthError as error:
            report.error = f"administrator login failed: {login_message(error)}"
            return report

        step = "checking the account"
        if not await _user_exists(client, username):
            report.error = (
                f"no DSM account named {username}: create it first in DSM "
                "(Control Panel > User & Group > User > Create), then submit again"
            )
            return report

        step = "listing Drive team folders"
        drive_folders = await _drive_folders(client)
        report.available_folders = sorted(name for name, on in drive_folders.items() if on)
        # Check existing folders before writing anything.
        unknown = [
            name
            for name in team_folders
            if name != new_folder and name not in report.available_folders
        ]
        if unknown:
            report.error = (
                f"not a Drive team folder: {', '.join(unknown)}. "
                f"Available: {', '.join(report.available_folders) or 'none'}"
            )
            return report

        if new_folder:
            step = "creating the shared folder"
            if new_folder not in await _shared_folders(client):
                volume = await _default_volume(client)
                info = {
                    "name": new_folder,
                    "vol_path": volume,
                    "desc": "Team folder for syno-workspace-mcp",
                    "enable_recycle_bin": True,
                    "enable_share_cow": False,
                    "enable_share_compress": False,
                    "name_org": "",
                }
                await client.call(
                    "SYNO.Core.Share",
                    "create",
                    1,
                    {"name": _json(new_folder), "shareinfo": _json(info)},
                    http_method="POST",
                )
            report.folder_created = True

            step = "enabling the Drive team folder"
            if not drive_folders.get(new_folder):
                setting = {
                    "share_name": new_folder,
                    "rotate_cnt": 8,
                    "rotate_policy": "fifo",
                    "rotate_days": 30,
                    "share_type": "normal",
                    "share_enable": True,
                }
                await client.call(
                    "SYNO.SynologyDrive.Share",
                    "set",
                    1,
                    {"share": _json([setting])},
                    http_method="POST",
                )
            report.folder_enabled = True

        step = "granting folder access"
        permissions = [
            {
                "name": name,
                "is_readonly": False,
                "is_writable": True,
                "is_deny": False,
                "is_custom": False,
            }
            for name in folders
        ]
        if permissions:
            await client.call(
                "SYNO.Core.Share.Permission",
                "set_by_user_group",
                1,
                {
                    "name": _json(username),
                    "user_group_type": _json("local_user"),
                    "permissions": _json(permissions),
                },
                http_method="POST",
            )
        report.folders_granted = True

        step = "allowing Synology Drive"
        rule = {
            "entity_type": "user",
            "entity_name": username,
            "app_id": DRIVE_APP_ID,
            "allow_ip": ["0.0.0.0"],  # noqa: S104 - DSM's "any address", as its UI sends
        }
        await client.call(
            "SYNO.Core.AppPriv.Rule", "set", 1, {"rules": _json([rule])}, http_method="POST"
        )
        report.drive_allowed = True
    except SynologyError as error:
        report.error = f"{step} failed: {error}"
        return report
    finally:
        await client.aclose()

    try:
        await check_account(dsm_url, verify_tls, username, password)
    except SetupError as error:
        report.error = str(error)
        return report
    report.account_checked = True
    return report
