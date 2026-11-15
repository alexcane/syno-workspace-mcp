"""Local registry mapping a display name (and optional Drive path) to a Spreadsheet ID.

The Spreadsheet API only knows the alphanumeric ID found in the Office URL
(`/oo/r/<id>`); Drive file IDs cannot be converted to it. The registry lets tools
address a spreadsheet by name. It is a small JSON file in `DATA_DIR`, rewritten
atomically on every change.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from syno_workspace_mcp.errors import InvalidInputError, RegistryError

REGISTRY_FILE = "spreadsheet-ids.json"
REGISTRY_VERSION: Literal[1] = 1

# Mixed-case letters and digits, per the Spreadsheet API documentation.
_SPREADSHEET_ID = re.compile(r"[A-Za-z0-9]{8,128}")
_URL_ID = re.compile(r"/oo/r/([A-Za-z0-9]+)")

Source = Literal["create", "manual"]


class RegistryEntry(BaseModel):
    name: str
    path: str | None = Field(description="Drive path used to tell same-name files apart")
    spreadsheet_id: str
    source: Source
    registered_at: str = Field(description="ISO 8601")


class _RegistryFile(BaseModel):
    version: Literal[1] = REGISTRY_VERSION
    entries: list[RegistryEntry] = Field(default_factory=list)


def parse_spreadsheet_id(value: str) -> str:
    """Accept a bare ID or an Office URL such as `https://host/oo/r/<id>#tid=1`."""
    stripped = value.strip()
    match = _URL_ID.search(stripped)
    candidate = match[1] if match else stripped
    if candidate.isdigit():
        raise InvalidInputError(
            "this looks like a Drive file ID, not a Spreadsheet ID: open the file in "
            "Synology Office and take the ID after /oo/r/ in its URL"
        )
    if not _SPREADSHEET_ID.fullmatch(candidate):
        raise InvalidInputError(
            "not a Spreadsheet ID: expected the letters and digits after /oo/r/ "
            "in the Office URL, or the full URL"
        )
    return candidate


class SpreadsheetRegistry:
    """JSON-backed registry; every call reads the file so external edits are seen."""

    def __init__(self, data_dir: Path) -> None:
        self._file = data_dir / REGISTRY_FILE

    @property
    def file(self) -> Path:
        return self._file

    def list(self) -> list[RegistryEntry]:
        return list(self._load().entries)

    def resolve(self, name: str, path: str | None = None) -> str:
        matches = [entry for entry in self._load().entries if entry.name == name]
        if path is not None:
            matches = [entry for entry in matches if entry.path == path]
        if not matches:
            target = f"'{name}'" if path is None else f"'{name}' at '{path}'"
            raise RegistryError(
                f"spreadsheet {target} is not registered; call spreadsheet_register with "
                "the ID from the Office URL (/oo/r/<id>), or pass file_id"
            )
        if len(matches) > 1:
            candidates = ", ".join(entry.path or "(no path)" for entry in matches)
            raise RegistryError(
                f"several spreadsheets are registered as '{name}'; pass path to choose "
                f"one of: {candidates}"
            )
        return matches[0].spreadsheet_id

    def find_by_path(self, path: str) -> RegistryEntry | None:
        return next((entry for entry in self._load().entries if entry.path == path), None)

    def register(
        self, name: str, spreadsheet_id: str, *, path: str | None, source: Source
    ) -> RegistryEntry:
        """Add or replace the entry for (name, path)."""
        if not name.strip():
            raise InvalidInputError("name must not be empty")
        data = self._load()
        entry = RegistryEntry(
            name=name.strip(),
            path=path,
            spreadsheet_id=spreadsheet_id,
            source=source,
            registered_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        data.entries = [e for e in data.entries if (e.name, e.path) != (entry.name, path)]
        data.entries.append(entry)
        self._save(data)
        return entry

    def unregister(self, spreadsheet_id: str) -> bool:
        data = self._load()
        kept = [entry for entry in data.entries if entry.spreadsheet_id != spreadsheet_id]
        if len(kept) == len(data.entries):
            return False
        data.entries = kept
        self._save(data)
        return True

    def _load(self) -> _RegistryFile:
        try:
            raw = self._file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _RegistryFile()
        except OSError as error:
            raise RegistryError(f"cannot read {self._file}: {error.strerror}") from error
        try:
            return _RegistryFile.model_validate_json(raw)
        except ValidationError as error:
            # Never start from an empty registry here: the next write would wipe it.
            raise RegistryError(
                f"{self._file} is not a valid registry file; fix or remove it"
            ) from error

    def _save(self, data: _RegistryFile) -> None:
        directory = self._file.parent
        payload = json.dumps(data.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=directory, prefix=".spreadsheet-ids.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._file)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as error:
            raise RegistryError(f"cannot write {self._file}: {error.strerror}") from error
