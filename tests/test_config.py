from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from syno_workspace_mcp.config import Settings


def load() -> Settings:
    return Settings()


def test_defaults(env: dict[str, str]) -> None:
    settings = load()

    assert str(settings.syno_dsm_url) == "https://nas.example.test:5001/"
    assert settings.syno_verify_tls is True
    assert str(settings.syno_ss_api_url) == "http://127.0.0.1:3000/"
    assert settings.syno_ss_dsm_url == settings.syno_dsm_url
    assert settings.syno_modules == ["drive", "spreadsheet"]
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 3100
    assert settings.mcp_allowed_hosts == ["drive.example.test"]
    assert settings.data_dir == Path(env["DATA_DIR"])
    assert settings.log_level == "INFO"
    assert settings.listens_on_loopback is True


@pytest.mark.parametrize(
    "missing", ["SYNO_DSM_URL", "SYNO_USERNAME", "SYNO_PASSWORD", "MCP_AUTH_TOKEN"]
)
def test_required_variables(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError) as exc_info:
        load()

    assert exc_info.value.errors()[0]["loc"] == (missing.lower(),)


def test_short_token_is_rejected(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "too-short")

    with pytest.raises(ValidationError, match="at least 32 characters"):
        load()


def test_csv_lists_are_parsed(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNO_MODULES", " drive , drive,spreadsheet ")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "a.example.test, b.example.test")

    settings = load()

    assert settings.syno_modules == ["drive", "spreadsheet"]
    assert settings.mcp_allowed_hosts == ["a.example.test", "b.example.test"]


def test_unknown_module_is_rejected(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNO_MODULES", "drive,mailplus")

    with pytest.raises(ValidationError):
        load()


def test_empty_modules_is_rejected(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNO_MODULES", "")

    with pytest.raises(ValidationError, match="at least one module"):
        load()


def test_explicit_ss_dsm_url_is_kept(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNO_SS_DSM_URL", "https://127.0.0.1:5001")

    assert str(load().syno_ss_dsm_url) == "https://127.0.0.1:5001/"


def test_log_level_is_case_insensitive(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert load().log_level == "DEBUG"


@pytest.mark.parametrize(
    ("host", "loopback"),
    [("127.0.0.1", True), ("::1", True), ("localhost", True), ("0.0.0.0", False)],  # noqa: S104
)
def test_listens_on_loopback(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, host: str, loopback: bool
) -> None:
    monkeypatch.setenv("MCP_HOST", host)

    assert load().listens_on_loopback is loopback


def test_secrets_are_not_in_repr(settings: Settings, env: dict[str, str]) -> None:
    text = repr(settings)

    assert env["SYNO_PASSWORD"] not in text
    assert env["MCP_AUTH_TOKEN"] not in text
