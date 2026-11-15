from __future__ import annotations

import logging
import sys

import pytest

from syno_workspace_mcp.logging import REDACTED, RedactionFilter, redact, setup_logging

SECRET = "s3cr3t-value"


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        ("GET /webapi/entry.cgi?api=SYNO.API.Auth&passwd=hunter2&account=x", "hunter2"),
        ("password=hunter2", "hunter2"),
        ('{"password": "hunter2"}', "hunter2"),
        ("cookie _sid=abcDEF123", "abcDEF123"),
        ("sid: abcDEF123", "abcDEF123"),
        ("SynoToken=tok123", "tok123"),
        ("X-SYNO-TOKEN: tok123", "tok123"),
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("headers={'authorization': 'Bearer abc.def'}", "abc.def"),
        ("sending bearer abc.def-ghi upstream", "abc.def-ghi"),
    ],
)
def test_redact_known_keys(raw: str, leak: str) -> None:
    result = redact(raw)

    assert leak not in result
    assert REDACTED in result


def test_redact_known_secret_values_anywhere() -> None:
    assert redact(f"value is {SECRET}!", [SECRET]) == f"value is {REDACTED}!"


def test_redact_keeps_unrelated_text() -> None:
    text = "listing /mydrive/labs (consider=yes, side=left)"

    assert redact(text) == text


def test_filter_redacts_args_and_tracebacks() -> None:
    record_filter = RedactionFilter([SECRET])
    try:
        raise RuntimeError(f"login failed passwd={SECRET}")
    except RuntimeError:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "token %s", (SECRET,), exc_info=None
        )
        record.exc_info = sys.exc_info()

    assert record_filter.filter(record) is True
    assert SECRET not in record.getMessage()
    assert record.exc_text is not None
    assert SECRET not in record.exc_text
    assert "RuntimeError" in record.exc_text


def test_no_secret_reaches_captured_logs(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("DEBUG", [SECRET])
    try:
        for name in ("syno_workspace_mcp", "fastmcp", "uvicorn.access", "httpx"):
            logging.getLogger(name).warning("auth header Authorization: Bearer %s", SECRET)
        logging.getLogger("syno_workspace_mcp").info("raw secret %s", SECRET)
    finally:
        logging.getLogger().handlers.clear()

    err = capsys.readouterr().err
    assert err.count("WARNING") == 4
    assert SECRET not in err
