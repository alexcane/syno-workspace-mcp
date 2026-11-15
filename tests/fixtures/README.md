# DSM fixtures

Synthetic DSM responses, shaped after the upstream test mocks
(`vocweb/synology-mcp-server`, DSM 7.3.2). No real NAS data: names, IDs and
paths are invented. Replace them with anonymised recordings from the smoke
tests when the real shapes differ.

The write fixtures (`drive_create_folder`, `drive_upload`, `drive_labels`,
`drive_rename`, `drive_task_move_finished`, `drive_sharing_create`) follow the
responses recorded from the Drive web UI (package 4.0.3) on the real NAS,
trimmed and anonymised: names, IDs, paths, links and the owner are invented.

The Spreadsheet API fixtures (`ss_*.json`) follow the response schemas of the
`synology/spreadsheet-api` container's OpenAPI document (API 3.4.1, served at
`/openapi.yml`). Values are invented; replace them with anonymised recordings
from `tests/smoke/test_spreadsheet_smoke.py` where the real shapes differ.
