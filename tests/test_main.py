from __future__ import annotations

import pytest

from syno_workspace_mcp.__main__ import main


def test_invalid_config_exits_with_clear_message(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SYNO_PASSWORD")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")

    assert main([]) == 2

    err = capsys.readouterr().err
    assert "Invalid configuration:" in err
    assert "SYNO_PASSWORD: Field required" in err
    assert "MCP_AUTH_TOKEN" in err
    assert "at least 32 characters" in err


def test_placeholder_is_named_without_echoing_the_value(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SYNO_PASSWORD", "CHANGE_ME_DSM_PASSWORD")

    assert main([]) == 2

    err = capsys.readouterr().err
    assert "SYNO_PASSWORD: Value error, still holds a CHANGE_ME placeholder" in err
    assert "CHANGE_ME_DSM_PASSWORD" not in err
