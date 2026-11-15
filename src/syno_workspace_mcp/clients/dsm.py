"""DSM Web API client: login, session reuse, transparent re-login on expiry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from syno_workspace_mcp.errors import (
    SESSION_EXPIRED_CODES,
    DsmApiError,
    DsmAuthError,
    DsmNetworkError,
    InvalidInputError,
    message_for_code,
)
from syno_workspace_mcp.logging import forget_secret, register_secret

logger = logging.getLogger(__name__)

AUTH_PATH = "/webapi/auth.cgi"
ENTRY_PATH = "/webapi/entry.cgi"
AUTH_API = "SYNO.API.Auth"
AUTH_VERSION = 6
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
TRANSFER_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

Params = dict[str, str | int | bool]


@dataclass(frozen=True)
class DsmUploadFile:
    name: str
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class DsmDownload:
    content: bytes
    content_type: str
    content_disposition: str


@dataclass(frozen=True)
class _Session:
    sid: str
    syno_token: str | None

    def headers(self) -> dict[str, str]:
        # `id` is DSM's session cookie name; the SynoToken guards against CSRF
        # when the session travels by cookie.
        headers = {"Cookie": f"id={self.sid}"}
        if self.syno_token:
            headers["X-SYNO-TOKEN"] = self.syno_token
        return headers


def _encode(params: Params) -> dict[str, str]:
    # DSM expects lowercase booleans; httpx would send `True`/`False`.
    return {
        key: ("true" if value else "false") if isinstance(value, bool) else str(value)
        for key, value in params.items()
    }


class DsmClient:
    """One shared HTTP client and one DSM session for the whole server.

    The session is opened lazily on the first call. When DSM reports it expired
    (codes 106 / 108 / 119), the client logs in again once and replays the request once.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: SecretStr,
        *,
        verify_tls: bool = True,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._username = username
        self._password = password
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=False,
        )
        self._session: _Session | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> DsmClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Log out (best effort) and release the HTTP connections."""
        try:
            await self.logout()
        finally:
            await self._http.aclose()

    # Public API -------------------------------------------------------------

    async def call(
        self,
        api: str,
        method: str,
        version: int,
        params: Params | None = None,
        *,
        http_method: Literal["GET", "POST"] = "GET",
    ) -> dict[str, Any]:
        """Call `api.method` on `entry.cgi` and return the `data` object."""
        query: Params = {"api": api, "version": version, "method": method, **(params or {})}
        return await self._with_session(
            lambda session: self._send(api, query, session, http_method)
        )

    async def upload(
        self,
        api: str,
        method: str,
        version: int,
        fields: dict[str, str],
        file: DsmUploadFile,
        *,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Send a multipart upload; `api`/`method`/`version` travel in the query string."""
        if len(file.content) > max_bytes:
            raise InvalidInputError(f"file is larger than the {max_bytes} bytes limit", api=api)
        query: Params = {"api": api, "version": version, "method": method}
        return await self._with_session(
            lambda session: self._send_upload(api, query, fields, file, session)
        )

    async def download(
        self, api: str, method: str, version: int, params: Params, *, max_bytes: int
    ) -> DsmDownload:
        """GET a binary payload, refusing anything larger than `max_bytes`."""
        query: Params = {"api": api, "version": version, "method": method, **params}
        return await self._with_session(
            lambda session: self._send_download(api, query, session, max_bytes)
        )

    async def logout(self) -> None:
        async with self._lock:
            session, self._session = self._session, None
        if session is None:
            return
        try:
            await self._http.post(
                AUTH_PATH,
                data={
                    "api": AUTH_API,
                    "version": str(AUTH_VERSION),
                    "method": "logout",
                    "_sid": session.sid,
                },
            )
        except httpx.HTTPError as error:
            logger.warning("DSM logout failed: %s", type(error).__name__)
        finally:
            forget_secret(session.sid)
            if session.syno_token:
                forget_secret(session.syno_token)

    # Session handling -------------------------------------------------------

    async def _with_session[T](self, send: Callable[[_Session], Awaitable[T]]) -> T:
        session = await self._get_session()
        try:
            return await send(session)
        except DsmApiError as error:
            if error.code not in SESSION_EXPIRED_CODES:
                raise
            logger.info("DSM session expired (code %s), logging in again", error.code)
            session = await self._renew_session(session)
            return await send(session)

    async def _get_session(self) -> _Session:
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is None:
                self._session = await self._login()
            return self._session

    async def _renew_session(self, stale: _Session) -> _Session:
        async with self._lock:
            # Another task may already have replaced the stale session.
            if self._session is None or self._session == stale:
                self._session = await self._login()
            return self._session

    async def _login(self) -> _Session:
        # Credentials go in the form body, never in the URL.
        form = {
            "api": AUTH_API,
            "version": str(AUTH_VERSION),
            "method": "login",
            "account": self._username,
            "passwd": self._password.get_secret_value(),
            "format": "sid",
            "enable_syno_token": "yes",
        }
        payload = await self._post_json(AUTH_PATH, form, AUTH_API)
        if not payload.get("success"):
            code = _error_code(payload)
            raise DsmAuthError(message_for_code(code, AUTH_API), code=code, api=AUTH_API)

        data = payload.get("data")
        sid = data.get("sid") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise DsmAuthError("DSM login succeeded but returned no session id", api=AUTH_API)
        token = data.get("synotoken") if isinstance(data, dict) else None
        syno_token = token if isinstance(token, str) and token else None

        register_secret(sid)
        if syno_token:
            register_secret(syno_token)
        logger.info("logged in to DSM as %s", self._username)
        return _Session(sid=sid, syno_token=syno_token)

    # HTTP -------------------------------------------------------------------

    async def _send(
        self,
        api: str,
        query: Params,
        session: _Session,
        http_method: Literal["GET", "POST"],
    ) -> dict[str, Any]:
        try:
            if http_method == "GET":
                response = await self._http.get(
                    ENTRY_PATH, params=_encode(query), headers=session.headers()
                )
            else:
                response = await self._http.post(
                    ENTRY_PATH, data=_encode(query), headers=session.headers()
                )
        except httpx.TimeoutException as error:
            raise DsmNetworkError(f"DSM did not answer in time ({api})", api=api) from error
        except httpx.HTTPError as error:
            raise DsmNetworkError(f"cannot reach DSM ({type(error).__name__})", api=api) from error

        return _data(_parse(response, api), api)

    async def _send_upload(
        self,
        api: str,
        query: Params,
        fields: dict[str, str],
        file: DsmUploadFile,
        session: _Session,
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(
                ENTRY_PATH,
                params=_encode(query),
                data=fields,
                files={"file": (file.name, file.content, file.mime_type)},
                headers=session.headers(),
                timeout=TRANSFER_TIMEOUT,
            )
        except httpx.TimeoutException as error:
            raise DsmNetworkError(f"DSM did not answer in time ({api})", api=api) from error
        except httpx.HTTPError as error:
            raise DsmNetworkError(f"cannot reach DSM ({type(error).__name__})", api=api) from error
        return _data(_parse(response, api), api)

    async def _send_download(
        self, api: str, query: Params, session: _Session, max_bytes: int
    ) -> DsmDownload:
        try:
            async with self._http.stream(
                "GET",
                ENTRY_PATH,
                params=_encode(query),
                headers=session.headers(),
                timeout=TRANSFER_TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    raise DsmNetworkError(
                        f"DSM answered HTTP {response.status_code} for {api}", api=api
                    )
                content_type = response.headers.get("content-type", "application/octet-stream")
                if "application/json" in content_type:
                    # With `json_error=true` DSM reports failures as a JSON envelope.
                    await response.aread()
                    _data(_parse(response, api), api)
                    raise DsmNetworkError(f"DSM returned JSON instead of a file for {api}", api=api)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise DsmNetworkError(
                            f"file is larger than the {max_bytes} bytes limit", api=api
                        )
                    chunks.append(chunk)
                return DsmDownload(
                    content=b"".join(chunks),
                    content_type=content_type,
                    content_disposition=response.headers.get("content-disposition", ""),
                )
        except httpx.TimeoutException as error:
            raise DsmNetworkError(f"DSM did not answer in time ({api})", api=api) from error
        except httpx.HTTPError as error:
            raise DsmNetworkError(f"cannot reach DSM ({type(error).__name__})", api=api) from error

    async def _post_json(self, path: str, form: dict[str, str], api: str) -> dict[str, Any]:
        try:
            response = await self._http.post(path, data=form)
        except httpx.TimeoutException as error:
            raise DsmNetworkError(f"DSM did not answer in time ({api})", api=api) from error
        except httpx.HTTPError as error:
            raise DsmNetworkError(f"cannot reach DSM ({type(error).__name__})", api=api) from error
        return _parse(response, api)


def _parse(response: httpx.Response, api: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise DsmNetworkError(f"DSM answered HTTP {response.status_code} for {api}", api=api)
    try:
        payload = response.json()
    except ValueError as error:
        raise DsmNetworkError(f"DSM returned a non-JSON response for {api}", api=api) from error
    if not isinstance(payload, dict):
        raise DsmNetworkError(f"unexpected DSM response for {api}", api=api)
    return payload


def _data(payload: dict[str, Any], api: str) -> dict[str, Any]:
    if not payload.get("success"):
        code = _error_code(payload)
        raise DsmApiError(message_for_code(code, api), code=code, api=api)
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise DsmNetworkError(f"unexpected DSM response for {api}: data is not an object")
    return data


def _error_code(payload: dict[str, Any]) -> int:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else 100
