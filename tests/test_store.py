from __future__ import annotations

import json
from pathlib import Path

import pytest

from syno_workspace_mcp.errors import InvalidInputError, RegistryError
from syno_workspace_mcp.store import REGISTRY_FILE, SpreadsheetRegistry, parse_spreadsheet_id

SID = "AbCdEf1234567890GhIjKl"
OTHER = "ZyXwVu0987654321TsRqPo"


@pytest.mark.parametrize(
    "value",
    [
        SID,
        f"  {SID} ",
        f"https://drive.example.test:8586/oo/r/{SID}",
        f"https://drive.example.test/oo/r/{SID}#tid=1",
    ],
)
def test_parse_spreadsheet_id_accepts_id_and_url(value: str) -> None:
    assert parse_spreadsheet_id(value) == SID


@pytest.mark.parametrize("value", ["", "abc", "../etc/passwd", "882614125167948800 x", "a/b/c"])
def test_parse_spreadsheet_id_rejects_garbage(value: str) -> None:
    with pytest.raises(InvalidInputError):
        parse_spreadsheet_id(value)


def test_parse_spreadsheet_id_rejects_drive_file_id() -> None:
    with pytest.raises(InvalidInputError, match="Drive file ID"):
        parse_spreadsheet_id("882614125167948800")


def test_register_and_resolve(tmp_path: Path) -> None:
    registry = SpreadsheetRegistry(tmp_path)

    entry = registry.register("labs", SID, path="/team-folders/labs/x.osheet", source="manual")

    assert entry.spreadsheet_id == SID
    assert registry.resolve("labs") == SID
    assert registry.resolve("labs", "/team-folders/labs/x.osheet") == SID
    assert registry.find_by_path("/team-folders/labs/x.osheet") == entry


def test_registry_survives_a_new_instance(tmp_path: Path) -> None:
    SpreadsheetRegistry(tmp_path).register("labs", SID, path=None, source="create")

    assert SpreadsheetRegistry(tmp_path).resolve("labs") == SID
    stored = json.loads((tmp_path / REGISTRY_FILE).read_text())
    assert stored["version"] == 1
    assert [e["spreadsheet_id"] for e in stored["entries"]] == [SID]
    assert [p.name for p in tmp_path.iterdir()] == [REGISTRY_FILE]


def test_register_replaces_same_name_and_path(tmp_path: Path) -> None:
    registry = SpreadsheetRegistry(tmp_path)
    registry.register("labs", SID, path=None, source="manual")
    registry.register("labs", OTHER, path=None, source="manual")

    assert registry.resolve("labs") == OTHER
    assert len(registry.list()) == 1


def test_duplicate_names_need_a_path(tmp_path: Path) -> None:
    registry = SpreadsheetRegistry(tmp_path)
    registry.register("budget", SID, path="/mydrive/a.osheet", source="manual")
    registry.register("budget", OTHER, path="/mydrive/b.osheet", source="manual")

    with pytest.raises(RegistryError, match=r"/mydrive/a\.osheet, /mydrive/b\.osheet"):
        registry.resolve("budget")
    assert registry.resolve("budget", "/mydrive/b.osheet") == OTHER


def test_unknown_name_points_to_register(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="spreadsheet_register"):
        SpreadsheetRegistry(tmp_path).resolve("missing")


def test_unregister(tmp_path: Path) -> None:
    registry = SpreadsheetRegistry(tmp_path)
    registry.register("labs", SID, path=None, source="manual")

    assert registry.unregister(SID) is True
    assert registry.unregister(SID) is False
    assert registry.list() == []


def test_corrupt_file_is_reported_and_never_overwritten(tmp_path: Path) -> None:
    file = tmp_path / REGISTRY_FILE
    file.write_text("{not json")
    registry = SpreadsheetRegistry(tmp_path)

    with pytest.raises(RegistryError, match="not a valid registry"):
        registry.register("labs", SID, path=None, source="manual")
    assert file.read_text() == "{not json"


def test_missing_data_dir_is_created(tmp_path: Path) -> None:
    registry = SpreadsheetRegistry(tmp_path / "nested" / "data")

    registry.register("labs", SID, path=None, source="manual")

    assert registry.file.exists()
