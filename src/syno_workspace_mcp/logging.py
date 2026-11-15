"""Logging setup with a redaction filter applied to every record."""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Iterable

REDACTED = "***"

# Keys whose values must never be logged, whatever the syntax around them:
# `passwd=x`, `"password": "x"`, `_sid: x`, `SynoToken=x`, `Authorization: Bearer x`.
_SENSITIVE_KEYS = r"passwd|password|_sid|sid|synotoken|x-syno-token|authorization"
_KEY_VALUE = re.compile(
    rf"""(?P<key>["']?\b(?:{_SENSITIVE_KEYS})\b["']?\s*[:=]\s*)"""
    r"""(?P<quote>["']?)"""
    r"""(?:(?:bearer|basic)\s+)?"""
    r"""(?P<value>[^\s"'&,;}\]]+)""",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(bearer)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)

# Loggers that install their own handlers or stop propagation; they are routed
# through the root handler so the filter sees their records too.
_THIRD_PARTY_LOGGERS = ("fastmcp", "mcp", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _KEY_VALUE.sub(lambda m: f"{m['key']}{m['quote']}{REDACTED}", text)
    return _BEARER.sub(lambda m: f"{m[1]} {REDACTED}", text)


class RedactionFilter(logging.Filter):
    """Rewrite each record so neither its message nor its traceback leaks secrets."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            formatted = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact(formatted.rstrip("\n"), self._secrets)
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._secrets)
        return True


def setup_logging(level: str, secrets: Iterable[str] = ()) -> None:
    """Send all logs to stderr through a single redacting handler."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter(secrets))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        for existing in third_party.handlers[:]:
            third_party.removeHandler(existing)
        third_party.propagate = True
        third_party.setLevel(logging.NOTSET)
