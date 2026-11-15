# syno-workspace-mcp

MCP server for **Synology Drive** and **Synology Spreadsheet**, built with
[FastMCP](https://gofastmcp.com). Python port of
[vocweb/synology-mcp-server](https://github.com/vocweb/synology-mcp-server).

> **Status: milestone 4 (Spreadsheet).** The 11 Drive and 15 Spreadsheet tools are
> available. Tools that overwrite, rename or delete do nothing unless called with
> `confirm: true`.

## How it runs

- Streamable HTTP transport on `/mcp`, listening on loopback (`127.0.0.1:3100`).
- The DSM reverse proxy terminates HTTPS and forwards to it.
- Clients authenticate with a static bearer token (`MCP_AUTH_TOKEN`).
- `GET /health` is unauthenticated and only returns `{"status": "ok"}`.
- Host header validation: only loopback and `MCP_ALLOWED_HOSTS` are accepted
  (other hosts get `421`); browser requests from foreign origins get `403`.

## Install on a Synology NAS

Requires Container Manager (Docker) and SSH access.

```bash
mkdir -p /volume1/docker/syno-workspace-mcp && cd /volume1/docker/syno-workspace-mcp
curl -fsSLO https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/main/deploy/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/main/deploy/.env.example
vi .env   # fill in DSM URL, account, secrets (openssl rand -hex 32), allowed hosts
mkdir -p data && sudo chown 1000:1000 data
sudo docker compose pull && sudo docker compose up -d
curl -fsS http://127.0.0.1:3100/health
```

Then add a DSM reverse proxy rule (HTTPS → `http://localhost:3100`, read timeout
3600 s) and connect a client:

```bash
claude mcp add --scope user --transport http synology https://<host>:<port>/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

## Spreadsheet tools

Spreadsheet tools go through the `synology/spreadsheet-api` container (started by
the same compose file), which logs in to DSM itself at `SYNO_SS_DSM_URL`.

- The DSM account needs the **Synology Office** application privilege in addition
  to Synology Drive.
- A spreadsheet is addressed by its Spreadsheet ID (`file_id`): the letters and
  digits after `/oo/r/` in the Office URL, also listed by `spreadsheet_list`. The
  full URL is accepted, Drive file IDs are not. The Drive `path` of the `.osheet`
  works too, and so does a `name` stored with `spreadsheet_register` (kept in
  `DATA_DIR`; `spreadsheet_create` registers the files it creates).
- `spreadsheet_read_sheet` returns formula results; `include_formulas: true`
  returns the formulas as typed. Dates are serial day numbers.
- `spreadsheet_create` cannot pick a folder: the Spreadsheet API decides where
  the file goes; move it with `drive_move_file`.

## Configuration

All configuration comes from environment variables, validated at startup; see
[`deploy/.env.example`](deploy/.env.example). An invalid or missing variable stops
the server with a message naming it.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest --cov
```

Run locally over stdio (for MCP Inspector or a desktop client):

```bash
uv run python -m syno_workspace_mcp --transport stdio
```

`tests/smoke/` holds tests against a real NAS; they are excluded from the default
run and launched by hand.

## Releases

Pushing a tag `vX.Y.Z` matching `version` in `pyproject.toml` builds the
`linux/amd64` image and publishes `ghcr.io/alexcane/syno-workspace-mcp:X.Y.Z`.
No `latest` tag is published.

## License

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
