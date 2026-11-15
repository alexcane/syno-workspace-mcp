from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from syno_workspace_mcp.clients.dsm import DsmClient
from syno_workspace_mcp.errors import DsmApiError, DsmAuthError, DsmNetworkError
from syno_workspace_mcp.logging import RedactionFilter

from .conftest import DSM_URL, TEST_PASSWORD, FakeDsm

API = "SYNO.SynologyDrive.Files"
OK = httpx.Response(200, json={"success": True, "data": {"value": 1}})


def syno_error(code: int) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "error": {"code": code}})


@pytest.fixture
async def dsm(fake_dsm: FakeDsm) -> AsyncIterator[DsmClient]:
    client = DsmClient(DSM_URL + "/", "mcp_service", SecretStr(TEST_PASSWORD))
    yield client
    await client.aclose()


async def test_login_is_lazy_and_keeps_credentials_out_of_urls(
    dsm: DsmClient, fake_dsm: FakeDsm
) -> None:
    fake_dsm.entry.mock(return_value=OK)
    assert fake_dsm.logins == []

    data = await dsm.call(API, "list", 2, {"path": "/mydrive", "limit": 10, "recursive": False})

    assert data == {"value": 1}
    login = fake_dsm.auth.calls[0].request
    assert TEST_PASSWORD not in str(login.url)
    assert fake_dsm.logins[0]["passwd"] == TEST_PASSWORD
    assert fake_dsm.logins[0]["format"] == "sid"
    assert fake_dsm.logins[0]["enable_syno_token"] == "yes"

    entry = fake_dsm.entry.calls[0].request
    assert entry.method == "GET"
    assert entry.headers["Cookie"] == "id=SID-fixture-0001"
    assert entry.headers["X-SYNO-TOKEN"] == "TOKEN-fixture-0001"
    assert "SID-fixture-0001" not in str(entry.url)
    assert fake_dsm.entry_params() == {
        "api": API,
        "version": "2",
        "method": "list",
        "path": "/mydrive",
        "limit": "10",
        "recursive": "false",
    }


async def test_session_is_reused(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=OK)

    await dsm.call(API, "list", 2)
    await dsm.call(API, "get", 2)

    assert len(fake_dsm.logins) == 1


async def test_concurrent_calls_share_one_login(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=OK)

    await asyncio.gather(*(dsm.call(API, "list", 2) for _ in range(5)))

    assert len(fake_dsm.logins) == 1
    assert fake_dsm.entry.call_count == 5


async def test_post_sends_parameters_in_body(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=OK)

    await dsm.call(API, "create", 2, {"path": "/mydrive/x"}, http_method="POST")

    request = fake_dsm.entry.calls[0].request
    assert request.method == "POST"
    assert "path=%2Fmydrive%2Fx" in request.content.decode()
    assert request.url.params.get("path") is None


async def test_response_without_data_is_empty(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=httpx.Response(200, json={"success": True}))

    assert await dsm.call(API, "list", 2) == {}


@pytest.mark.parametrize("code", [106, 108, 119])
async def test_expired_session_relogs_once_and_replays(
    dsm: DsmClient, fake_dsm: FakeDsm, code: int
) -> None:
    fake_dsm.login_responses.append(
        {"success": True, "data": {"sid": "SID-fixture-0002", "synotoken": "TOKEN-2"}}
    )
    fake_dsm.entry.mock(side_effect=[OK, syno_error(code), OK])

    await dsm.call(API, "list", 2)
    data = await dsm.call(API, "list", 2)

    assert data == {"value": 1}
    assert len(fake_dsm.logins) == 2
    assert fake_dsm.entry.calls[-1].request.headers["Cookie"] == "id=SID-fixture-0002"


async def test_second_expiry_is_raised(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(side_effect=[syno_error(119), syno_error(106)])

    with pytest.raises(DsmApiError) as caught:
        await dsm.call(API, "list", 2)

    assert caught.value.code == 106
    assert len(fake_dsm.logins) == 2
    assert fake_dsm.entry.call_count == 2


async def test_concurrent_expiries_relog_once(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.login_responses.append({"success": True, "data": {"sid": "SID-fixture-0002"}})

    def expire_first_session(request: httpx.Request) -> httpx.Response:
        if request.headers["Cookie"] == "id=SID-fixture-0001":
            return syno_error(119)
        return OK

    fake_dsm.entry.mock(side_effect=expire_first_session)

    results = await asyncio.gather(*(dsm.call(API, "list", 2) for _ in range(3)))

    assert results == [{"value": 1}] * 3
    assert len(fake_dsm.logins) == 2


async def test_other_errors_do_not_relogin(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=syno_error(105))

    with pytest.raises(DsmApiError, match="permission denied") as caught:
        await dsm.call(API, "list", 2)

    assert caught.value.code == 105
    assert caught.value.api == API
    assert "(DSM code 105)" in str(caught.value)
    assert len(fake_dsm.logins) == 1


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (400, "SYNO_USERNAME / SYNO_PASSWORD"),
        (401, "disabled"),
        (403, "2FA"),
        (404, "2FA"),
        (407, "auto-block"),
        (999, "SYNO.API.Auth returned error code 999"),
    ],
)
async def test_login_errors_are_actionable(
    dsm: DsmClient, fake_dsm: FakeDsm, code: int, hint: str
) -> None:
    fake_dsm.login_responses = [{"success": False, "error": {"code": code}}]

    with pytest.raises(DsmAuthError, match=hint) as caught:
        await dsm.call(API, "list", 2)

    assert caught.value.code == code
    assert fake_dsm.entry.call_count == 0


async def test_login_without_error_code_is_unknown(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.login_responses = [{"success": False}]

    with pytest.raises(DsmAuthError) as caught:
        await dsm.call(API, "list", 2)

    assert caught.value.code == 100


async def test_login_without_sid_fails(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.login_responses = [{"success": True, "data": {}}]

    with pytest.raises(DsmAuthError, match="no session id"):
        await dsm.call(API, "list", 2)


async def test_login_without_synotoken_omits_header(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.login_responses = [{"success": True, "data": {"sid": "SID-only"}}]
    fake_dsm.entry.mock(return_value=OK)

    await dsm.call(API, "list", 2)

    assert "X-SYNO-TOKEN" not in fake_dsm.entry.calls[0].request.headers


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(502), "HTTP 502"),
        (httpx.Response(200, text="<html>"), "non-JSON"),
        (httpx.Response(200, json=[1, 2]), "unexpected DSM response"),
        (httpx.Response(200, json={"success": True, "data": [1]}), "data is not an object"),
    ],
)
async def test_malformed_responses(
    dsm: DsmClient, fake_dsm: FakeDsm, response: httpx.Response, match: str
) -> None:
    fake_dsm.entry.mock(return_value=response)

    with pytest.raises(DsmNetworkError, match=match):
        await dsm.call(API, "list", 2)


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (httpx.ConnectError("refused"), "cannot reach DSM"),
        (httpx.ReadTimeout("slow"), "did not answer in time"),
    ],
)
async def test_transport_errors_on_calls(
    dsm: DsmClient, fake_dsm: FakeDsm, error: Exception, match: str
) -> None:
    fake_dsm.entry.mock(side_effect=error)

    with pytest.raises(DsmNetworkError, match=match):
        await dsm.call(API, "list", 2)


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (httpx.ConnectError("refused"), "cannot reach DSM"),
        (httpx.ConnectTimeout("slow"), "did not answer in time"),
    ],
)
async def test_transport_errors_on_login(
    dsm: DsmClient, fake_dsm: FakeDsm, error: Exception, match: str
) -> None:
    fake_dsm.auth.mock(side_effect=error)

    with pytest.raises(DsmNetworkError, match=match):
        await dsm.call(API, "list", 2)


async def test_logout_sends_sid_in_body(fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=OK)
    async with DsmClient(DSM_URL, "mcp_service", SecretStr(TEST_PASSWORD)) as dsm:
        await dsm.call(API, "list", 2)

    assert len(fake_dsm.logouts) == 1
    assert fake_dsm.logouts[0]["_sid"] == "SID-fixture-0001"
    assert "SID-fixture-0001" not in str(fake_dsm.auth.calls[-1].request.url)


async def test_no_logout_without_session(fake_dsm: FakeDsm) -> None:
    async with DsmClient(DSM_URL, "mcp_service", SecretStr(TEST_PASSWORD)):
        pass

    assert fake_dsm.auth.call_count == 0


async def test_logout_failure_is_swallowed(dsm: DsmClient, fake_dsm: FakeDsm) -> None:
    fake_dsm.entry.mock(return_value=OK)
    await dsm.call(API, "list", 2)
    fake_dsm.auth.mock(side_effect=httpx.ConnectError("gone"))

    await dsm.logout()
    await dsm.logout()  # second call is a no-op

    assert fake_dsm.auth.call_count == 2  # login + failed logout


async def test_session_secrets_are_redacted_while_logged_in(
    dsm: DsmClient, fake_dsm: FakeDsm
) -> None:
    fake_dsm.entry.mock(return_value=OK)
    await dsm.call(API, "list", 2)
    record_filter = RedactionFilter()

    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "%s %s", ("SID-fixture-0001", "TOKEN-fixture-0001"), None
    )
    record_filter.filter(record)

    assert "SID-fixture-0001" not in record.getMessage()
    assert "TOKEN-fixture-0001" not in record.getMessage()


async def test_no_secret_in_logs_during_session_lifecycle(
    fake_dsm: FakeDsm, caplog: pytest.LogCaptureFixture
) -> None:
    fake_dsm.entry.mock(side_effect=[syno_error(119), OK])
    caplog.handler.addFilter(RedactionFilter([TEST_PASSWORD]))
    caplog.set_level(logging.DEBUG)

    async with DsmClient(DSM_URL, "mcp_service", SecretStr(TEST_PASSWORD)) as dsm:
        await dsm.call(API, "list", 2)

    text = caplog.text
    assert "logged in to DSM" in text
    for secret in (TEST_PASSWORD, "SID-fixture-0001", "TOKEN-fixture-0001"):
        assert secret not in text
