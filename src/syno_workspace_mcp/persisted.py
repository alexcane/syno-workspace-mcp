"""Configuration written by the `/setup` page into `DATA_DIR`.

Two files, both keyed by `Settings` field names:

- `config.json`: non-secret values (DSM URL, account name, allowed hosts...).
- `secrets.json`: DSM password and MCP token, mode 600.

On Synology, `DATA_DIR` usually carries an inherited ACL (the Container Manager
guide grants Everyone read/write on it). `chmod` drops that ACL, so the secrets
file is restricted *before* anything is written to it, then moved into place.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_FILE = "config.json"
SECRETS_FILE = "secrets.json"
SECRET_KEYS = frozenset({"syno_password", "mcp_auth_token"})


class PersistedConfigError(Exception):
    """A file in `DATA_DIR` exists but cannot be read as a JSON object."""


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise PersistedConfigError(f"cannot read {path}: {error.strerror}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PersistedConfigError(f"{path} is not valid JSON (line {error.lineno})") from error
    if not isinstance(data, dict):
        raise PersistedConfigError(f"{path} must contain a JSON object")
    return data


def load(data_dir: Path) -> dict[str, Any]:
    """Merged content of both files; missing files count as empty."""
    return {**_read(data_dir / CONFIG_FILE), **_read(data_dir / SECRETS_FILE)}


def exists(data_dir: Path) -> bool:
    return (data_dir / CONFIG_FILE).exists() or (data_dir / SECRETS_FILE).exists()


def _write_atomic(path: Path, data: dict[str, Any], mode: int) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # Restrict first: on an ACL folder the new file inherits the ACL.
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def save(data_dir: Path, values: dict[str, Any]) -> None:
    """Split `values` between the two files and write them atomically."""
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets = {key: value for key, value in values.items() if key in SECRET_KEYS}
    config = {key: value for key, value in values.items() if key not in SECRET_KEYS}
    _write_atomic(data_dir / SECRETS_FILE, secrets, 0o600)
    _write_atomic(data_dir / CONFIG_FILE, config, 0o644)
