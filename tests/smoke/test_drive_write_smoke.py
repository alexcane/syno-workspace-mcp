"""Manual smoke test of the Drive write operations against a real DSM.

Works in a throwaway folder `mcp-smoke-<timestamp>` under SMOKE_DRIVE_PATH
(default `/team-folders/labs`) and moves it to the Drive trash at the end.
Run with the real environment loaded, e.g.:
    uv run pytest tests/smoke/test_drive_write_smoke.py -o addopts="" -v -s
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from syno_workspace_mcp.clients.drive import DriveClient
from syno_workspace_mcp.clients.dsm import DsmClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("SYNO_DSM_URL"), reason="SYNO_DSM_URL not set: no real NAS to talk to"
)

SMOKE_PATH = os.environ.get("SMOKE_DRIVE_PATH", "/team-folders/labs")
CONTENT = b"syno-workspace-mcp smoke test\n"


@pytest.fixture
async def drive() -> AsyncIterator[DriveClient]:
    async with DsmClient(
        os.environ["SYNO_DSM_URL"],
        os.environ["SYNO_USERNAME"],
        SecretStr(os.environ["SYNO_PASSWORD"]),
        verify_tls=os.environ.get("SYNO_VERIFY_TLS", "true").lower() != "false",
    ) as dsm:
        yield DriveClient(dsm)


async def test_write_cycle(drive: DriveClient) -> None:
    root = f"{SMOKE_PATH}/mcp-smoke-{int(time.time())}"

    # force_parent: `root` does not exist yet, so both levels get created.
    nested = await drive.create_folder(root, "sub", force_parent=True)
    assert nested.folder_path == f"{root}/sub"
    print(f"created {nested.folder_path}")
    try:
        uploaded = await drive.upload(root, "smoke.txt", CONTENT, mime_type="text/plain")
        assert uploaded.file_path == f"{root}/smoke.txt"

        versioned = await drive.upload(root, "smoke.txt", CONTENT, conflict_action="version")
        assert versioned.file_id == uploaded.file_id
        renamed_copy = await drive.upload(root, "smoke.txt", CONTENT, conflict_action="autorename")
        assert renamed_copy.file_path != uploaded.file_path
        print(f"autorename gave {renamed_copy.file_name}")

        downloaded = await drive.download(uploaded.file_path)
        assert downloaded.content == CONTENT
        print(f"downloaded {downloaded.file_name} ({downloaded.mime_type})")

        labels = await drive.list_labels()
        print(f"labels: {[label.name for label in labels]}")
        if labels:
            await drive.add_label(uploaded.file_id, labels[0].name)
            info = await drive.get_file_info(uploaded.file_id)
            assert labels[0].name in info.labels

        link = await drive.get_sharing_link(
            uploaded.file_path, permission="view", password="smoke-pass", expire_days=1
        )
        assert link.link.startswith("http")
        assert link.expires_at is not None
        relinked = await drive.get_sharing_link(uploaded.file_path, permission="download")
        assert relinked.link == link.link
        print(f"sharing link OK ({link.link.split('/d/')[0]}/d/...)")

        moved = await drive.move(uploaded.file_id, nested.folder_id, new_name="moved.txt")
        assert moved.new_path == f"{root}/sub/moved.txt"

        renamed = await drive.move(moved.new_path, nested.folder_path, new_name="renamed.txt")
        assert renamed.new_path == f"{root}/sub/renamed.txt"

        await drive.delete(renamed.new_path)
        listing = await drive.list_files(nested.folder_path)
        assert listing.total == 0
    finally:
        await drive.delete(root)
        print(f"moved {root} to the trash")
