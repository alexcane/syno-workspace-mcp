"""Manual smoke tests against a real DSM (excluded from the default run).

Run with the real environment loaded, e.g.:
    uv run pytest tests/smoke -o addopts="" -v
Optional: SMOKE_DRIVE_PATH (default `/team-folders/labs`).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from syno_workspace_mcp.clients.drive import DriveClient
from syno_workspace_mcp.clients.dsm import DsmClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("SYNO_DSM_URL"), reason="SYNO_DSM_URL not set: no real NAS to talk to"
)

SMOKE_PATH = os.environ.get("SMOKE_DRIVE_PATH", "/team-folders/labs")


@pytest.fixture
async def drive() -> AsyncIterator[DriveClient]:
    async with DsmClient(
        os.environ["SYNO_DSM_URL"],
        os.environ["SYNO_USERNAME"],
        SecretStr(os.environ["SYNO_PASSWORD"]),
        verify_tls=os.environ.get("SYNO_VERIFY_TLS", "true").lower() != "false",
    ) as dsm:
        yield DriveClient(dsm)


async def test_list_smoke_path(drive: DriveClient) -> None:
    listing = await drive.list_files(SMOKE_PATH, limit=20)

    assert listing.total >= len(listing.files)
    print(f"{SMOKE_PATH}: {listing.total} entries")
    for entry in listing.files:
        print(f"  {entry.type:4} {entry.path}")


async def test_get_info_and_search(drive: DriveClient) -> None:
    info = await drive.get_file_info(SMOKE_PATH)
    assert info.type == "dir"

    listing = await drive.list_files(SMOKE_PATH, limit=1, file_type="file")
    if listing.files:
        by_id = await drive.get_file_info(listing.files[0].id)
        assert by_id.id == listing.files[0].id
        found = await drive.search(listing.files[0].name, folder_path=SMOKE_PATH)
        assert any(f.id == by_id.id for f in found.files)
