"""Manual smoke tests against the real Spreadsheet API container (excluded by default).

The container listens on the NAS loopback only, so open a tunnel first:
    ssh -N -L 3000:127.0.0.1:3000 syno

Then, with the real environment loaded:
    SMOKE_SS_ID=<id from /oo/r/<id>> uv run pytest tests/smoke/test_spreadsheet_smoke.py \
        -o addopts="" -v -s

- `SMOKE_SS_ID`: Spreadsheet ID of an existing file, only read (e.g. fichier_labs).
- `SMOKE_SS_WRITE=1`: also run the write scenario on a throwaway spreadsheet that is
  created, edited and deleted again (it never touches SMOKE_SS_ID).
- `SYNO_SS_API_URL` (default `http://127.0.0.1:3000`) and `SYNO_SS_DSM_URL` (default
  `SYNO_DSM_URL`): the DSM URL is the one the *container* calls back, seen from the NAS.

The tests print the raw shapes they get so differences with the OpenAPI document
can be recorded as fixtures.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from syno_workspace_mcp.clients.drive import DriveClient
from syno_workspace_mcp.clients.dsm import DsmClient
from syno_workspace_mcp.clients.spreadsheet import SpreadsheetClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("SYNO_DSM_URL") or not os.environ.get("SMOKE_SS_ID"),
    reason="SYNO_DSM_URL / SMOKE_SS_ID not set: no real Spreadsheet API to talk to",
)

SS_ID = os.environ.get("SMOKE_SS_ID", "")
SMOKE_PATH = os.environ.get("SMOKE_DRIVE_PATH", "/team-folders/labs")


def show(label: str, value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    print(f"\n--- {label}\n{text[:2000]}")


@pytest.fixture
async def ss() -> AsyncIterator[SpreadsheetClient]:
    dsm_url = os.environ.get("SYNO_SS_DSM_URL") or os.environ["SYNO_DSM_URL"]
    async with SpreadsheetClient(
        os.environ.get("SYNO_SS_API_URL", "http://127.0.0.1:3000"),
        dsm_url,
        os.environ["SYNO_USERNAME"],
        SecretStr(os.environ["SYNO_PASSWORD"]),
    ) as client:
        yield client


@pytest.fixture
async def dsm() -> AsyncIterator[DsmClient]:
    async with DsmClient(
        os.environ["SYNO_DSM_URL"],
        os.environ["SYNO_USERNAME"],
        SecretStr(os.environ["SYNO_PASSWORD"]),
        verify_tls=os.environ.get("SYNO_VERIFY_TLS", "true").lower() != "false",
    ) as client:
        yield client


async def test_read_existing_spreadsheet(ss: SpreadsheetClient) -> None:
    info = await ss.get_info(SS_ID)
    show("get_info", info.model_dump())
    assert info.sheets

    first = min(info.sheets, key=lambda sheet: sheet.index)
    cells = await ss.get_values(SS_ID, first.name, "A1:E10")
    show("values A1:E10", cells.model_dump())

    styles = await ss.get_styles(SS_ID, first.name, "A1:B2")
    show("styles A1:B2", styles.model_dump())

    csv = await ss.export(SS_ID, "csv", sheet_id=first.sheet_id)
    show("csv export", {"file_name": csv.file_name, "mime": csv.mime_type})
    show("csv content", csv.content.decode(errors="replace"))


async def test_drive_raw_osheet_fields(dsm: DsmClient) -> None:
    """Does Drive expose the Spreadsheet ID of an .osheet? (would avoid manual register)."""
    drive = DriveClient(dsm)
    listing = await drive.list_files(SMOKE_PATH, file_type="file", pattern="*.osheet", limit=5)
    osheets = [f for f in listing.files if f.extension == "osheet"]
    if not osheets:
        pytest.skip(f"no .osheet in {SMOKE_PATH}")
    raw = await dsm.call("SYNO.SynologyDrive.Files", "get", 2, {"path": f"id:{osheets[0].id}"})
    show(f"raw Files.get of {osheets[0].path}", raw)
    print(f"\nSpreadsheet ID found in the raw Drive record: {SS_ID in json.dumps(raw)}")


@pytest.mark.skipif(os.environ.get("SMOKE_SS_WRITE") != "1", reason="SMOKE_SS_WRITE != 1")
async def test_write_scenario_on_throwaway_spreadsheet(
    ss: SpreadsheetClient, dsm: DsmClient
) -> None:
    drive = DriveClient(dsm)
    name = f"mcp-smoke-{int(time.time())}"
    new_id = await ss.create(name)
    show("created", {"name": name, "id": new_id})
    try:
        # Where does `create` put the file, and is its permanent_link the Spreadsheet ID?
        found = await drive.search(name, folder_path="/mydrive")
        show("Drive search for the new file", [f.model_dump() for f in found.files])
        for file in found.files:
            _, link = await drive.get_with_link(file.id)
            print(f"\npermanent_link of {file.path}: {link} (== new id: {link == new_id})")

        info = await ss.get_info(new_id)
        sheet = info.sheets[0]
        start_rows = sheet.row_count

        await ss.write_values(
            new_id, sheet.name, "A1", [["Name", "Qty", "Total"], ["a", 2, "=B2*10"]]
        )
        appended = await ss.append_values(new_id, sheet.name, [["b", 3, "=B3*10"]])
        show("append", appended.model_dump())

        # One row inserted at index 0: expect row_count + 1 and 'Name' moved to A2.
        await ss.batch_update(new_id, sheet.sheet_id, "insert_rows", 0, 1)
        after_insert = await ss.get_info(new_id)
        column = await ss.get_values(new_id, sheet.name, "A1:A3")
        show(
            "insert_rows count=1",
            {
                "row_count": f"{start_rows} -> {after_insert.sheets[0].row_count}",
                "A1:A3": column.model_dump(),
            },
        )

        # One row deleted at index 0: expect row_count - 1 and 'Name' back in A1.
        await ss.batch_update(new_id, sheet.sheet_id, "delete_rows", 0, 1)
        after_delete = await ss.get_info(new_id)
        column = await ss.get_values(new_id, sheet.name, "A1:A3")
        show(
            "delete_rows count=1",
            {
                "row_count": f"{after_insert.sheets[0].row_count} -> "
                f"{after_delete.sheets[0].row_count}",
                "A1:A3": column.model_dump(),
            },
        )
        assert column.values[0] == ["Name"]

        # Sheet names with a space: does the unquoted 'Mon onglet!A1' range work?
        added = await ss.add_sheet(new_id, "Mon onglet")
        await ss.write_values(new_id, "Mon onglet", "B2", [["espace"]])
        spaced = await ss.get_values(new_id, "Mon onglet", "A1:C3")
        show("sheet with a space", {"added": added.model_dump(), "values": spaced.model_dump()})
        await ss.rename_sheet(new_id, added.sheet_id, "Renamed")
        await ss.delete_sheet(new_id, added.sheet_id)
        show("final info", (await ss.get_info(new_id)).model_dump())
    finally:
        deleted = await ss.delete(new_id)
        show("deleted", deleted)
        # Deleted for good, or in the Drive recycle bin?
        left = await drive.search(name, folder_path="/mydrive")
        show("Drive search after delete", [f.path for f in left.files])
