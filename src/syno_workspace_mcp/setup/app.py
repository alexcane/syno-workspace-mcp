"""The /setup page: a 4-step wizard served when the server has no configuration.

Steps: unlock with the code printed in the log, choose how Claude reaches the
server, connect (or create) the DSM account, then save. Saving writes `DATA_DIR`
and stops the process; Docker's restart policy starts it again with the new
configuration, and /setup is no longer served.

FastMCP's auth does not cover custom routes and the allowed host names are not
known yet, so the page protects itself: setup code, one session cookie, a CSRF
token per form, same-origin check, 5 attempts per minute on the forms that check
secrets, and HTTPS through the DSM reverse proxy (or a direct loopback request).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from jinja2 import Environment
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from syno_workspace_mcp import persisted
from syno_workspace_mcp.logging import register_secret
from syno_workspace_mcp.setup.accounts import (
    PreparationReport,
    SetupError,
    check_account,
    parse_folder_name,
    prepare_service_account,
)
from syno_workspace_mcp.web import SECURITY_HEADERS, create_environment

logger = logging.getLogger(__name__)

SETUP_PATH = "/setup"
SESSION_COOKIE = "syno_setup"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 60.0
DEFAULT_ACCOUNT = "mcp_service"

# DSM rejects these characters in account names; the first one cannot be a space.
_FORBIDDEN = r"!\"#$%&'()*+,/:;<=>?@\[\]\\^`{}|~"
_USERNAME = re.compile(rf"[^{_FORBIDDEN}\s][^{_FORBIDDEN}]*")


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


@dataclass
class RateLimiter:
    """At most `limit` attempts in any `window` seconds (the page has one user)."""

    limit: int = MAX_ATTEMPTS
    window: float = ATTEMPT_WINDOW
    clock: Callable[[], float] = time.monotonic
    _attempts: deque[float] = field(default_factory=deque)

    def allow(self) -> bool:
        now = self.clock()
        while self._attempts and now - self._attempts[0] >= self.window:
            self._attempts.popleft()
        if len(self._attempts) >= self.limit:
            return False
        self._attempts.append(now)
        return True


@dataclass
class SetupState:
    code: str
    session_id: str | None = None
    csrf: str | None = None
    exposure: Literal["local", "public"] | None = None
    mcp_url: str | None = None
    dsm_url: str | None = None
    verify_tls: bool = True
    username: str | None = None
    password: SecretStr | None = None
    preparation: PreparationReport | None = None
    error: str | None = None
    finished: bool = False

    def reset_steps(self) -> None:
        self.exposure = self.mcp_url = self.dsm_url = None
        self.username = None
        self.password = None
        self.preparation = None
        self.verify_tls = True


# Request helpers --------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _forwarded_proto(request: Request) -> str | None:
    value = request.headers.get("x-forwarded-proto")
    return value.split(",")[0].strip().lower() if value else None


def _secure_transport(request: Request) -> bool:
    """HTTPS through the reverse proxy, or a direct request from the NAS itself."""
    proto = _forwarded_proto(request)
    if proto is not None:
        return proto == "https"
    return _is_loopback(request.client.host if request.client else None)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        # Browsers send Origin on form posts; non-browser clients still need the CSRF token.
        return origin is None
    return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()


def _request_host(request: Request) -> str:
    return request.headers.get("host", "nas.example.com")


def _hostname(netloc: str) -> str:
    return urlsplit(f"//{netloc}").hostname or netloc


def parse_mcp_url(raw: str) -> tuple[str, str]:
    """Return (normalized URL, host name) or raise `SetupError`."""
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise SetupError("the address must start with https:// and contain a host name")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SetupError("the address must not contain credentials, ? or #")
    path = parts.path.rstrip("/") or "/mcp"
    if path != "/mcp":
        raise SetupError("the address must end with /mcp")
    netloc = parts.netloc.lower()
    return f"https://{netloc}/mcp", parts.hostname.lower()


def parse_dsm_url(raw: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise SetupError("the DSM address must look like https://nas.example.com:5001")
    if parts.username or parts.password:
        raise SetupError("the DSM address must not contain credentials")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def parse_username(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 64 or not _USERNAME.fullmatch(value):
        raise SetupError("invalid DSM account name")
    return value


def parse_folders(raw: str) -> list[str]:
    names = [name.strip() for name in re.split(r"[,\n]", raw) if name.strip()]
    return list(dict.fromkeys(names))


# Application ------------------------------------------------------------------


@dataclass
class SetupContext:
    state: SetupState
    data_dir: Path
    on_finished: Callable[[], Awaitable[None] | None]
    limiter: RateLimiter = field(default_factory=RateLimiter)
    templates: Environment = field(
        default_factory=lambda: create_environment("syno_workspace_mcp.setup")
    )


def create_setup_app(
    data_dir: Path,
    *,
    code: str | None = None,
    on_finished: Callable[[], Awaitable[None] | None] = lambda: None,
    limiter: RateLimiter | None = None,
) -> Starlette:
    ctx = SetupContext(
        state=SetupState(code=code or generate_code()),
        data_dir=data_dir,
        on_finished=on_finished,
        limiter=limiter or RateLimiter(),
    )

    def render(name: str, status: int = 200, **values: Any) -> HTMLResponse:
        state = ctx.state
        error, state.error = state.error, None
        html = ctx.templates.get_template(name).render(state=state, error=error, **values)
        return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)

    def plain(status: int, text: str) -> Response:
        return Response(text, status_code=status, media_type="text/plain", headers=SECURITY_HEADERS)

    def back_to_setup() -> RedirectResponse:
        return RedirectResponse(SETUP_PATH, status_code=303, headers=SECURITY_HEADERS)

    def unlocked(request: Request) -> bool:
        session = request.cookies.get(SESSION_COOKIE)
        expected = ctx.state.session_id
        return bool(session and expected and hmac.compare_digest(session, expected))

    async def form_of(request: Request) -> dict[str, str]:
        form = await request.form()
        return {key: value for key, value in form.items() if isinstance(value, str)}

    def valid_csrf(form: dict[str, str]) -> bool:
        expected = ctx.state.csrf
        return bool(expected and hmac.compare_digest(form.get("csrf", ""), expected))

    def guard(
        handler: Callable[[Request], Awaitable[Response]], *, needs_session: bool = True
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            if not _secure_transport(request):
                return plain(403, "Open this page over HTTPS, through the DSM reverse proxy.")
            if ctx.state.finished:
                return render("done.html", restarting=True)
            if request.method == "POST":
                if not _same_origin(request):
                    return plain(403, "Cross-origin request refused.")
                if needs_session and not unlocked(request):
                    return back_to_setup()
            return await handler(request)

        return wrapped

    # Routes -------------------------------------------------------------------

    async def page(request: Request) -> Response:
        state = ctx.state
        if not unlocked(request):
            return render("unlock.html")
        if state.exposure is None:
            return render("exposure.html", suggested_url=f"https://{_request_host(request)}/mcp")
        if state.password is None:
            suggested = state.dsm_url or f"https://{_hostname(_request_host(request))}:5001"
            return render(
                "dsm.html",
                suggested_dsm_url=suggested,
                default_account=state.username or DEFAULT_ACCOUNT,
                preparation=state.preparation,
            )
        return render("summary.html")

    async def unlock(request: Request) -> Response:
        form = await form_of(request)
        if not ctx.limiter.allow():
            ctx.state.error = "Too many attempts: wait one minute."
            return back_to_setup()
        if not hmac.compare_digest(normalize_code(form.get("code", "")), ctx.state.code):
            ctx.state.error = "Wrong setup code."
            return back_to_setup()
        ctx.state.session_id = secrets.token_urlsafe(32)
        ctx.state.csrf = secrets.token_urlsafe(32)
        response = back_to_setup()
        response.set_cookie(
            SESSION_COOKIE,
            ctx.state.session_id,
            path=SETUP_PATH,
            httponly=True,
            secure=_forwarded_proto(request) == "https",
            samesite="strict",
        )
        logger.info("setup page unlocked")
        return response

    async def exposure(request: Request) -> Response:
        form = await form_of(request)
        if not valid_csrf(form):
            return plain(403, "Invalid form token: reload the page.")
        mode = form.get("mode")
        if mode not in {"local", "public"}:
            ctx.state.error = "Choose how Claude will reach the server."
            return back_to_setup()
        if mode == "public" and form.get("public_ack") != "yes":
            ctx.state.error = "Tick every prerequisite before using a public address."
            return back_to_setup()
        try:
            url, _ = parse_mcp_url(form.get("mcp_url", ""))
        except SetupError as error:
            ctx.state.error = str(error)
            return back_to_setup()
        ctx.state.exposure = mode  # type: ignore[assignment]
        ctx.state.mcp_url = url
        return back_to_setup()

    def dsm_fields(form: dict[str, str]) -> tuple[str, bool, str]:
        dsm_url = parse_dsm_url(form.get("dsm_url", ""))
        verify_tls = form.get("verify_tls") == "yes"
        username = parse_username(form.get("username", DEFAULT_ACCOUNT))
        ctx.state.dsm_url, ctx.state.verify_tls, ctx.state.username = dsm_url, verify_tls, username
        return dsm_url, verify_tls, username

    async def dsm_check(request: Request) -> Response:
        form = await form_of(request)
        if not valid_csrf(form):
            return plain(403, "Invalid form token: reload the page.")
        if not ctx.limiter.allow():
            ctx.state.error = "Too many attempts: wait one minute."
            return back_to_setup()
        try:
            dsm_url, verify_tls, username = dsm_fields(form)
            password = SecretStr(form.get("password", ""))
            if not password.get_secret_value():
                raise SetupError("enter the account password")
            register_secret(password.get_secret_value())
            await check_account(dsm_url, verify_tls, username, password)
        except SetupError as error:
            ctx.state.error = str(error)
            return back_to_setup()
        ctx.state.password = password
        ctx.state.preparation = None
        return back_to_setup()

    async def dsm_prepare(request: Request) -> Response:
        form = await form_of(request)
        if not valid_csrf(form):
            return plain(403, "Invalid form token: reload the page.")
        if not ctx.limiter.allow():
            ctx.state.error = "Too many attempts: wait one minute."
            return back_to_setup()
        admin_password = SecretStr(form.get("admin_password", ""))
        password = SecretStr(form.get("password", ""))
        otp = form.get("otp_code", "").strip() or None
        try:
            dsm_url, verify_tls, username = dsm_fields(form)
            admin_username = parse_username(form.get("admin_username", ""))
            folders = parse_folders(form.get("team_folders", ""))
            new_folder = None
            if form.get("folder_mode", "new") == "new":
                new_folder = parse_folder_name(form.get("new_folder", ""))
            elif not folders:
                raise SetupError("name at least one existing Drive team folder")
            if not password.get_secret_value():
                raise SetupError("enter the password of the account you created")
            if not admin_password.get_secret_value():
                raise SetupError("enter the administrator password")
        except SetupError as error:
            ctx.state.error = str(error)
            return back_to_setup()
        register_secret(admin_password.get_secret_value())
        register_secret(password.get_secret_value())
        report = await prepare_service_account(
            dsm_url=dsm_url,
            verify_tls=verify_tls,
            admin_username=admin_username,
            admin_password=admin_password,
            otp_code=otp,
            username=username,
            password=password,
            team_folders=folders,
            new_folder=new_folder,
        )
        del admin_password, otp
        if report.complete:
            ctx.state.password = password
            ctx.state.preparation = None
            logger.info("DSM account %s prepared", username)
        else:
            ctx.state.preparation = report
            ctx.state.error = report.error
            logger.warning(
                "DSM account preparation stopped: folder=%s/%s access=%s drive=%s login=%s",
                report.folder_created,
                report.folder_enabled,
                report.folders_granted,
                report.drive_allowed,
                report.account_checked,
            )
        return back_to_setup()

    async def restart_steps(request: Request) -> Response:
        form = await form_of(request)
        if not valid_csrf(form):
            return plain(403, "Invalid form token: reload the page.")
        ctx.state.reset_steps()
        return back_to_setup()

    async def finish(request: Request) -> Response:
        form = await form_of(request)
        if not valid_csrf(form):
            return plain(403, "Invalid form token: reload the page.")
        state = ctx.state
        if not (state.mcp_url and state.dsm_url and state.username and state.password):
            return back_to_setup()
        _, host = parse_mcp_url(state.mcp_url)
        token = secrets.token_urlsafe(32)  # 43 characters
        register_secret(token)
        persisted.save(
            ctx.data_dir,
            {
                "exposure": state.exposure,
                "mcp_url": state.mcp_url,
                "mcp_allowed_hosts": [host],
                "syno_dsm_url": state.dsm_url,
                "syno_verify_tls": state.verify_tls,
                "syno_username": state.username,
                "syno_password": state.password.get_secret_value(),
                "mcp_auth_token": token,
            },
        )
        state.finished = True
        logger.info("configuration saved to %s; restarting", ctx.data_dir)
        response = render("done.html", token=token, mcp_url=state.mcp_url, restarting=False)
        response.background = BackgroundTask(ctx.on_finished)
        return response

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "setup"})

    routes = [
        Route(SETUP_PATH, guard(page), methods=["GET"]),
        Route(f"{SETUP_PATH}/unlock", guard(unlock, needs_session=False), methods=["POST"]),
        Route(f"{SETUP_PATH}/exposure", guard(exposure), methods=["POST"]),
        Route(f"{SETUP_PATH}/dsm/check", guard(dsm_check), methods=["POST"]),
        Route(f"{SETUP_PATH}/dsm/prepare", guard(dsm_prepare), methods=["POST"]),
        Route(f"{SETUP_PATH}/restart", guard(restart_steps), methods=["POST"]),
        Route(f"{SETUP_PATH}/finish", guard(finish), methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.setup = ctx
    return app
