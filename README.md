# syno-workspace-mcp

MCP server for **Synology Drive** and **Synology Spreadsheet**, built with
[FastMCP](https://gofastmcp.com). Python port of
[vocweb/synology-mcp-server](https://github.com/vocweb/synology-mcp-server).

> **Status: v1.** The 11 Drive and 15 Spreadsheet tools are available. Tools that
> overwrite, rename or delete do nothing unless called with `confirm: true`.

## How it runs

- Streamable HTTP transport on `/mcp`, listening on loopback (`127.0.0.1:3100`).
- The DSM reverse proxy terminates HTTPS and forwards to it.
- Clients authenticate with a static bearer token (`MCP_AUTH_TOKEN`).
- `GET /health` is unauthenticated and only returns `{"status": "ok"}`.
- Host header validation: only loopback and `MCP_ALLOWED_HOSTS` (or the address
  entered in `/setup`) are accepted
  (other hosts get `421`); browser requests from foreign origins get `403`.

## Install on a Synology NAS

No terminal? Follow the step-by-step
[Container Manager guide](docs/install-container-manager.md), done entirely in
the DSM web interface: once the containers run, a `/setup` page served through
the DSM reverse proxy prepares the DSM account and generates the access token.

The commands below require Container Manager (Docker) and SSH access.

```bash
mkdir -p /volume1/docker/syno-workspace-mcp && cd /volume1/docker/syno-workspace-mcp
curl -fsSLO https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/main/deploy/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/main/deploy/.env.example
vi .env   # fill in DSM URL, account, secrets (openssl rand -hex 32), allowed hosts
mkdir -p data && sudo chown 1000:1000 data
sudo /usr/local/bin/docker compose pull && sudo /usr/local/bin/docker compose up -d   # docker is not on sudo's PATH on DSM
curl -fsS http://127.0.0.1:3100/health
```

Then add a DSM reverse proxy rule (*Control Panel → Login Portal → Advanced →
Reverse Proxy*): source HTTPS `<host>:<port>`, destination `http://localhost:3100`,
HTTP 1.1, connection/send/read timeouts 3600 s. Add `<host>` to
`MCP_ALLOWED_HOSTS`.

## Connect a client

Claude Code:

```bash
claude mcp add --scope user --transport http synology https://<host>:<port>/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

### Claude Desktop extension

The easiest way for Claude Desktop: no terminal, nothing else to install.

1. Download `syno-workspace-mcp-<version>.mcpb` from the
   [latest release](https://github.com/alexcane/syno-workspace-mcp/releases/latest).
2. Double-click it (or drag it onto *Settings → Extensions*). Claude Desktop
   warns that the developer is not verified: the extension is not signed yet.
   macOS may then ask to let Claude access the local network: allow it if the
   server is on your LAN.
3. Enter the server URL (`https://<host>:<port>/mcp`) and the access token
   (`MCP_AUTH_TOKEN`). The token field is marked sensitive: Claude Desktop masks
   it and stores it securely.

The extension is a small stdio-to-HTTP bridge run by the Node.js that ships with
Claude Desktop. It only accepts `https://` URLs (plain `http://` on localhost).

### Claude Desktop without the extension

Claude Desktop has no remote HTTP transport with a static header, so it goes
through the [`fastmcp-remote`](https://gofastmcp.com/docs/clients/fastmcp-remote.md)
stdio bridge (requires [uv](https://docs.astral.sh/uv/)). In
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "synology": {
      "command": "uvx",
      "args": [
        "fastmcp-remote", "https://<host>:<port>/mcp",
        "--header", "Authorization: Bearer <MCP_AUTH_TOKEN>"
      ]
    }
  }
}
```

If DSM uses a certificate from a private CA, add `"--verify", "/path/to/ca.pem"`.

### Other MCP clients

Any client that supports remote MCP servers over Streamable HTTP with a custom
header can connect: URL `https://<host>:<port>/mcp`, header
`Authorization: Bearer <MCP_AUTH_TOKEN>`. Clients that only accept OAuth for
remote servers cannot use the static token.

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

The Claude Desktop extension lives in [`mcpb/`](mcpb) (Node.js ≥ 18):

```bash
cd mcpb && npm ci
npm test       # node:test, against an in-process MCP server
npm run pack   # dist/syno-workspace-mcp-<version>.mcpb, production deps only
```

## Privacy

The server and the extension send nothing to third parties. The extension only
talks to the server URL you enter; the server only talks to your own DSM and to
the Spreadsheet container on the NAS. Files and spreadsheet contents go to your AI
assistant only when a tool returns them.

## Releases

Pushing a tag `vX.Y.Z` matching `version` in `pyproject.toml` builds the
`linux/amd64` image and publishes `ghcr.io/alexcane/syno-workspace-mcp:X.Y.Z`.
No `latest` tag is published. The same tag attaches the `.mcpb` extension to the
GitHub release; `version` in `mcpb/manifest.json` and `mcpb/package.json` must
match too.

## License

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
