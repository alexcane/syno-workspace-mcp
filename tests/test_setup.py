from __future__ import annotations

import json
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from starlette.testclient import TestClient

from syno_workspace_mcp import persisted
from syno_workspace_mcp.__main__ import main
from syno_workspace_mcp.config import Settings
from syno_workspace_mcp.setup.app import (
    RateLimiter,
    create_setup_app,
    normalize_code,
    parse_mcp_url,
)

from .conftest import DSM_URL, TEST_PASSWORD, FakeDsm

CODE = "ABCD2345"
HOST = "drive.example.test:3443"
HTTPS = {"X-Forwarded-Proto": "https", "Host": HOST}
ADMIN_PASSWORD = "admin-password-for-tests"


# persisted -------------------------------------------------------------------


def test_save_splits_secrets_into_a_private_file(tmp_path: Path) -> None:
    persisted.save(
        tmp_path,
        {"syno_username": "mcp_service", "syno_password": "p", "mcp_auth_token": "t"},
    )

    secrets_file = tmp_path / persisted.SECRETS_FILE
    assert json.loads(secrets_file.read_text()) == {"mcp_auth_token": "t", "syno_password": "p"}
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert json.loads((tmp_path / persisted.CONFIG_FILE).read_text()) == {
        "syno_username": "mcp_service"
    }
    assert persisted.load(tmp_path)["syno_password"] == "p"
    assert not list(tmp_path.glob(".*.tmp"))


def test_corrupted_file_is_reported(tmp_path: Path) -> None:
    (tmp_path / persisted.CONFIG_FILE).write_text("{not json")

    with pytest.raises(persisted.PersistedConfigError, match="not valid JSON"):
        persisted.load(tmp_path)


def test_settings_read_saved_values_below_the_environment(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in ("SYNO_USERNAME", "SYNO_PASSWORD", "MCP_ALLOWED_HOSTS"):
        monkeypatch.delenv(key)
    persisted.save(
        tmp_path,
        {
            "syno_username": "from_file",
            "syno_password": "file-password",
            "mcp_allowed_hosts": ["drive.example.test"],
            "syno_dsm_url": "https://ignored.example.test:5001",
            "exposure": "local",
        },
    )

    settings = Settings()

    assert settings.syno_username == "from_file"
    assert settings.syno_password.get_secret_value() == "file-password"
    assert settings.mcp_allowed_hosts == ["drive.example.test"]
    # The environment wins over the file.
    assert str(settings.syno_dsm_url) == "https://nas.example.test:5001/"


# entry point -----------------------------------------------------------------


@pytest.fixture
def setup_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_run_setup(_: object, reason: str) -> None:
        calls.append(reason)

    monkeypatch.setattr("syno_workspace_mcp.setup.server.run_setup", fake_run_setup)
    return calls


def test_missing_configuration_starts_setup_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup_calls: list[str]
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key in ("SYNO_DSM_URL", "SYNO_USERNAME", "SYNO_PASSWORD", "MCP_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    assert main([]) == 0
    assert len(setup_calls) == 1
    assert "SYNO_PASSWORD" in setup_calls[0]


def test_setup_mode_variable_forces_setup(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, setup_calls: list[str]
) -> None:
    monkeypatch.setenv("SETUP_MODE", "true")

    assert main([]) == 0
    assert setup_calls == ["SETUP_MODE=true"]


def test_invalid_value_still_fails_instead_of_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup_calls: list[str]
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")

    assert main([]) == 2
    assert setup_calls == []


# helpers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://drive.example.test:3443/mcp", "https://drive.example.test:3443/mcp"),
        ("https://Drive.example.test:3443/", "https://drive.example.test:3443/mcp"),
    ],
)
def test_mcp_url_is_normalized(raw: str, expected: str) -> None:
    assert parse_mcp_url(raw)[0] == expected


@pytest.mark.parametrize(
    "raw", ["http://drive.example.test/mcp", "https://drive.example.test/other", "nonsense"]
)
def test_bad_mcp_url_is_rejected(raw: str) -> None:
    with pytest.raises(Exception, match="address"):
        parse_mcp_url(raw)


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = RateLimiter(limit=2, window=60, clock=lambda: now[0])

    assert limiter.allow()
    assert limiter.allow()
    assert not limiter.allow()
    now[0] = 61
    assert limiter.allow()


# page ------------------------------------------------------------------------


class DsmAdminFake:
    """Answers entry.cgi by `api.method`, records the calls, can fail one of them."""

    def __init__(self, fake_dsm: FakeDsm) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.users: set[str] = {"mcp_service"}
        self.shares: set[str] = {"labs"}
        self.drive_enabled: set[str] = {"labs"}
        self.fail: dict[str, int] = {}
        fake_dsm.entry.mock(side_effect=self._handle)
        self.fake_dsm = fake_dsm

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.content:
            params.update(parse_qsl(request.content.decode()))
        key = f"{params['api']}.{params['method']}"
        self.calls.append((key, params))
        if key in self.fail:
            return httpx.Response(200, json={"success": False, "error": {"code": self.fail[key]}})
        data: dict[str, Any] = {}
        if key == "SYNO.SynologyDrive.Share.list":
            data = {
                "items": [
                    {"share_name": name, "share_enable": name in self.drive_enabled}
                    for name in sorted(self.shares)
                ]
            }
        elif key == "SYNO.Core.Share.list":
            data = {"shares": [{"name": name} for name in sorted(self.shares)]}
        elif key == "SYNO.Core.Storage.Volume.list":
            data = {"volumes": [{"volume_path": "/volume1", "status": "normal", "readonly": False}]}
        elif key == "SYNO.Core.Share.create":
            self.shares.add(json.loads(params["name"]))
        elif key == "SYNO.SynologyDrive.Share.set":
            self.drive_enabled.update(s["share_name"] for s in json.loads(params["share"]))
        elif key == "SYNO.Core.User.get":
            if json.loads(params["name"]) not in self.users:
                return httpx.Response(200, json={"success": False, "error": {"code": 3106}})
            data = {"users": [{"name": json.loads(params["name"])}]}
        elif key == "SYNO.Core.User.create":
            self.users.add(json.loads(params["name"]))
        return httpx.Response(200, json={"success": True, "data": data})

    def api_calls(self) -> list[str]:
        return [key for key, _ in self.calls]


@pytest.fixture
def finished() -> list[bool]:
    return []


@pytest.fixture
def page(tmp_path: Path, finished: list[bool]) -> Iterator[TestClient]:
    app = create_setup_app(tmp_path, code=CODE, on_finished=lambda: finished.append(True))
    with TestClient(app, base_url=f"https://{HOST}") as client:
        client.headers.update(HTTPS)
        yield client


def csrf_of(client: TestClient) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text)
    assert match
    return match[1]


def unlock(client: TestClient) -> str:
    response = client.post("/setup/unlock", data={"code": "abcd-2345"})
    assert response.status_code == 200  # followed redirect
    return csrf_of(client)


def to_dsm_step(client: TestClient) -> str:
    csrf = unlock(client)
    client.post(
        "/setup/exposure",
        data={"csrf": csrf, "mode": "local", "mcp_url": f"https://{HOST}/mcp"},
    )
    return csrf


DSM_FIELDS = {"dsm_url": DSM_URL, "verify_tls": "yes", "username": "mcp_service"}


def test_page_requires_https_through_the_proxy(tmp_path: Path) -> None:
    app = create_setup_app(tmp_path, code=CODE)
    with TestClient(app, base_url="https://nas.example.test") as client:
        assert client.get("/setup").status_code == 403
        assert client.get("/setup", headers={"X-Forwarded-Proto": "http"}).status_code == 403
        assert client.get("/health").json() == {"status": "setup"}


@pytest.mark.parametrize(
    "pasted",
    [
        "ABCD2345",
        " abcd-2345 ",
        "2026-11-16 10:02:32,903 WARNING syno_workspace_mcp: Setup code: ABCD2345",
    ],
)
def test_normalize_code_accepts_the_whole_log_line(pasted: str) -> None:
    assert normalize_code(pasted) == CODE


def test_unlock_needs_the_code_and_is_rate_limited(page: TestClient) -> None:
    assert "Setup code" in page.get("/setup").text
    for _ in range(5):
        assert "Wrong setup code" in page.post("/setup/unlock", data={"code": "WRONG"}).text
    assert "Too many attempts" in page.post("/setup/unlock", data={"code": CODE}).text


def test_forms_need_session_csrf_and_same_origin(page: TestClient) -> None:
    # No session: back to the unlock form.
    response = page.post("/setup/exposure", data={"mode": "local"})
    assert "Setup code" in response.text

    csrf = unlock(page)
    assert page.post("/setup/exposure", data={"mode": "local"}).status_code == 403
    foreign = page.post(
        "/setup/exposure",
        data={"csrf": csrf, "mode": "local", "mcp_url": f"https://{HOST}/mcp"},
        headers={"Origin": "https://evil.example.test"},
    )
    assert foreign.status_code == 403
    opaque = page.post(
        "/setup/exposure",
        data={"csrf": csrf, "mode": "local", "mcp_url": f"https://{HOST}/mcp"},
        headers={"Origin": "null"},
    )
    assert opaque.status_code == 403
    same = page.post(
        "/setup/exposure",
        data={"csrf": csrf, "mode": "local", "mcp_url": f"https://{HOST}/mcp"},
        headers={"Origin": f"https://{HOST}"},
    )
    assert same.status_code == 200
    assert "syno_setup" in page.cookies
    headers = page.get("/setup").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    # `no-referrer` would make browsers send `Origin: null` and break every form.
    assert headers["referrer-policy"] == "same-origin"


def test_public_exposure_needs_the_acknowledgement(page: TestClient) -> None:
    csrf = unlock(page)
    response = page.post(
        "/setup/exposure",
        data={"csrf": csrf, "mode": "public", "mcp_url": "https://mcp.example.test/mcp"},
    )
    assert "Tick every prerequisite" in response.text


def test_existing_account_then_save(
    page: TestClient, fake_dsm: FakeDsm, tmp_path: Path, finished: list[bool]
) -> None:
    csrf = to_dsm_step(page)
    assert "Option A" in page.get("/setup").text

    response = page.post(
        "/setup/dsm/check", data={**DSM_FIELDS, "csrf": csrf, "password": TEST_PASSWORD}
    )
    assert "DSM accepted the account" in response.text
    assert fake_dsm.logins[0]["account"] == "mcp_service"

    done = page.post("/setup/finish", data={"csrf": csrf})

    token = re.search(r"<pre>([A-Za-z0-9_-]{40,})</pre>", done.text)
    assert token
    # Option B does not know the team folders: the assistant is asked to list them.
    assert "List my Drive team folders." in done.text
    saved = persisted.load(tmp_path)
    assert saved["mcp_auth_token"] == token[1]
    assert saved["syno_password"] == TEST_PASSWORD
    assert saved["mcp_allowed_hosts"] == ["drive.example.test"]
    assert saved["syno_dsm_url"] == DSM_URL
    assert finished == [True]
    assert "no longer available" in page.get("/setup").text


def test_wrong_account_password_is_explained(page: TestClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.login_responses = [{"success": False, "error": {"code": 400}}]
    csrf = to_dsm_step(page)

    response = page.post("/setup/dsm/check", data={**DSM_FIELDS, "csrf": csrf, "password": "x"})

    assert "wrong account name or password" in response.text


def prepare_form(csrf: str, **overrides: str) -> dict[str, str]:
    return {
        **DSM_FIELDS,
        "csrf": csrf,
        "password": TEST_PASSWORD,
        "folder_mode": "new",
        "new_folder": "workspace",
        "team_folders": "",
        "admin_username": "admin",
        "admin_password": ADMIN_PASSWORD,
        "otp_code": "123456",
        **overrides,
    }


def test_prepare_folder_and_account(page: TestClient, fake_dsm: FakeDsm, tmp_path: Path) -> None:
    dsm = DsmAdminFake(fake_dsm)
    csrf = to_dsm_step(page)

    response = page.post("/setup/dsm/prepare", data=prepare_form(csrf, team_folders="labs"))

    assert "DSM accepted the account" in response.text
    assert dsm.api_calls() == [
        "SYNO.Core.User.get",
        "SYNO.SynologyDrive.Share.list",
        "SYNO.Core.Share.list",
        "SYNO.Core.Storage.Volume.list",
        "SYNO.Core.Share.create",
        "SYNO.SynologyDrive.Share.set",
        "SYNO.Core.Share.Permission.set_by_user_group",
        "SYNO.Core.AppPriv.Rule.set",
    ]
    calls = dict(dsm.calls)
    assert json.loads(calls["SYNO.Core.Share.create"]["shareinfo"])["vol_path"] == "/volume1"
    permissions = json.loads(calls["SYNO.Core.Share.Permission.set_by_user_group"]["permissions"])
    assert [p["name"] for p in permissions] == ["workspace", "labs"]
    assert all(p["is_writable"] and not p["is_deny"] for p in permissions)
    rule = json.loads(calls["SYNO.Core.AppPriv.Rule.set"]["rules"])[0]
    assert rule["app_id"] == "SYNO.SDS.Drive.Application"
    assert rule["entity_name"] == "mcp_service"
    # Admin login carried the 2FA code and was logged out; then the account was tested.
    assert fake_dsm.logins[0]["account"] == "admin"
    assert fake_dsm.logins[0]["otp_code"] == "123456"
    assert fake_dsm.logins[1]["account"] == "mcp_service"
    assert "otp_code" not in fake_dsm.logins[1]
    assert len(fake_dsm.logouts) == 2

    done = page.post("/setup/finish", data={"csrf": csrf})
    assert "List the files in my Drive team folder workspace." in done.text
    saved = persisted.load(tmp_path)
    assert saved["syno_username"] == "mcp_service"
    assert saved["syno_password"] == TEST_PASSWORD
    assert ADMIN_PASSWORD not in json.dumps(saved)


def test_failed_step_is_reported_and_retry_is_safe(page: TestClient, fake_dsm: FakeDsm) -> None:
    dsm = DsmAdminFake(fake_dsm)
    dsm.fail["SYNO.Core.AppPriv.Rule.set"] = 105
    csrf = to_dsm_step(page)

    response = page.post("/setup/dsm/prepare", data=prepare_form(csrf))
    assert "allowing Synology Drive failed" in response.text
    assert "Folder access granted: yes" in response.text
    assert "Synology Drive allowed: no" in response.text

    del dsm.fail["SYNO.Core.AppPriv.Rule.set"]
    dsm.calls.clear()
    response = page.post("/setup/dsm/prepare", data=prepare_form(csrf))

    assert "DSM accepted the account" in response.text
    # The folder now exists and is enabled: nothing is created twice.
    assert "SYNO.Core.Share.create" not in dsm.api_calls()
    assert "SYNO.SynologyDrive.Share.set" not in dsm.api_calls()


def test_missing_account_asks_to_create_it_in_dsm(page: TestClient, fake_dsm: FakeDsm) -> None:
    dsm = DsmAdminFake(fake_dsm)
    dsm.users.clear()
    csrf = to_dsm_step(page)

    response = page.post("/setup/dsm/prepare", data=prepare_form(csrf))

    assert "no DSM account named mcp_service" in response.text
    assert dsm.api_calls() == ["SYNO.Core.User.get"]


def test_unknown_team_folder_stops_before_any_change(page: TestClient, fake_dsm: FakeDsm) -> None:
    dsm = DsmAdminFake(fake_dsm)
    csrf = to_dsm_step(page)

    response = page.post("/setup/dsm/prepare", data=prepare_form(csrf, team_folders="nope"))

    assert "not a Drive team folder: nope" in response.text
    assert "Available: labs" in response.text
    assert not any(k.endswith((".create", ".set", ".set_by_user_group")) for k in dsm.api_calls())
    assert len(fake_dsm.logouts) == 1


def test_wrong_password_of_the_new_account_is_reported(page: TestClient, fake_dsm: FakeDsm) -> None:
    DsmAdminFake(fake_dsm)
    fake_dsm.login_responses = [
        {"success": True, "data": {"sid": "admin-sid", "synotoken": "admin-token"}},
        {"success": False, "error": {"code": 400}},
    ]
    csrf = to_dsm_step(page)

    response = page.post("/setup/dsm/prepare", data=prepare_form(csrf))

    assert "DSM login failed for mcp_service: wrong account name or password" in response.text
    assert "Account login tested: no" in response.text


def test_admin_secrets_never_reach_the_logs(
    page: TestClient, fake_dsm: FakeDsm, caplog: pytest.LogCaptureFixture
) -> None:
    from syno_workspace_mcp.logging import RedactionFilter

    caplog.handler.addFilter(RedactionFilter())
    DsmAdminFake(fake_dsm)
    csrf = to_dsm_step(page)
    caplog.set_level("DEBUG")

    page.post("/setup/dsm/prepare", data=prepare_form(csrf))
    done = page.post("/setup/finish", data={"csrf": csrf})
    token_match = re.search(r"<pre>([A-Za-z0-9_-]{40,})</pre>", done.text)
    assert token_match

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert ADMIN_PASSWORD not in logged
    assert TEST_PASSWORD not in logged
    assert token_match[1] not in logged
