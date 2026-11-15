# DSM fixtures

Synthetic DSM responses, shaped after the upstream test mocks
(`vocweb/synology-mcp-server`, DSM 7.3.2). No real NAS data: names, IDs and
paths are invented. Replace them with anonymised recordings from the smoke
tests when the real shapes differ.

The write fixtures (`drive_create_folder`, `drive_upload`, `drive_labels`,
`drive_rename`, `drive_task_move_finished`, `drive_sharing_create`) follow the
responses recorded from the Drive web UI (package 4.0.3) on the real NAS,
trimmed and anonymised: names, IDs, paths, links and the owner are invented.
