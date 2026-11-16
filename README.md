# syno-workspace-mcp

Lets your AI assistant (Claude, ChatGPT, etc.) browse, read and edit the files
and spreadsheets of your own **Synology Drive** and **Synology Spreadsheet**. The
server runs in Docker on your NAS; nothing goes through a third-party service.

> **Status: v1.** 11 Drive and 15 Spreadsheet tools. Tools that overwrite,
> rename or delete do nothing unless called with `confirm: true`.

## Quick start

1. **Install on the NAS** with Container Manager, entirely in the DSM web
   interface: follow the [Container Manager guide](docs/install-container-manager.md)
   (about 20 minutes).
2. **Run the setup page** (`https://<host>:<port>/setup`): it prepares a
   dedicated DSM account and generates the access token. The guide walks you
   through it.
3. **Connect your AI assistant** with the server address and the access token:
   see [Connect a client](#connect-a-client).

Requirements: DSM 7.2 or later with Container Manager, Synology Drive Server and
Synology Office (for spreadsheets), and an HTTPS address for the NAS with a
valid certificate.

## Install with an AI agent

Once the server runs on the NAS (steps 1 and 2 above), an AI agent such as
Claude Code can connect Claude Code and Claude Desktop for you: it checks the
server, asks for the address and the access token, and configures the clients.
Paste this into your agent:

```text
Read https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/v1.2.2/llms-install.md and follow the instructions to connect syno-workspace-mcp.
```

[`llms-install.md`](llms-install.md) is plain text that humans can read too:
review it before giving it to an agent.

## Connect a client

The setup page shows the server address (`https://<host>:<port>/mcp`) and the
access token once, with these settings ready to copy.

### Claude Desktop extension

The easiest way for Claude Desktop: no terminal, nothing else to install.

1. Download `syno-workspace-mcp-<version>.mcpb` from the
   [latest release](https://github.com/alexcane/syno-workspace-mcp/releases/latest).
2. Double-click it (or drag it onto *Settings → Extensions*). Claude Desktop
   warns that the developer is not verified: the extension is not signed yet.
   macOS may then ask to let Claude access the local network: allow it if the
   server is on your LAN.
3. Enter the server URL and the access token. The token field is marked
   sensitive: Claude Desktop masks it and stores it securely.

The extension is a small stdio-to-HTTP bridge run by the Node.js that ships with
Claude Desktop. It only accepts `https://` URLs (plain `http://` on localhost).

<details>
<summary>Claude Desktop without the extension</summary>

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
        "--header", "Authorization: Bearer <access token>"
      ]
    }
  }
}
```

If DSM uses a certificate from a private CA, add `"--verify", "/path/to/ca.pem"`.

</details>

### Claude Code

```bash
claude mcp add --scope user --transport http synology https://<host>:<port>/mcp \
  --header "Authorization: Bearer <access token>"
```

### Other MCP clients

Any client that supports remote MCP servers over Streamable HTTP with a custom
header can connect: URL `https://<host>:<port>/mcp`, header
`Authorization: Bearer <access token>`. Clients that only accept OAuth for
remote servers cannot use the static token.

## Tools

| Drive | Spreadsheet |
| --- | --- |
| `drive_list_files`, `drive_get_file_info`, `drive_search_files`, `drive_download_file`, `drive_upload_file`, `drive_create_folder`, `drive_move_file`, `drive_delete_file`, `drive_get_sharing_link`, `drive_list_labels`, `drive_add_label` | `spreadsheet_list`, `spreadsheet_get_info`, `spreadsheet_read_sheet`, `spreadsheet_get_styles`, `spreadsheet_export`, `spreadsheet_register`, `spreadsheet_create`, `spreadsheet_add_sheet`, `spreadsheet_rename_sheet`, `spreadsheet_write_cells`, `spreadsheet_append_rows`, `spreadsheet_write_styles`, `spreadsheet_batch_update`, `spreadsheet_delete_sheet`, `spreadsheet_delete_file` |

The server only sees what its DSM account can open: team folders are under
`/team-folders/<name>`, and `drive_list_files` on `/team-folders` lists them.

### Spreadsheet notes

Spreadsheet tools go through the `synology/spreadsheet-api` container, started
by the same compose file, which logs in to DSM itself.

- A spreadsheet is addressed by its Spreadsheet ID (`file_id`): the letters and
  digits after `/oo/r/` in the Office URL, also listed by `spreadsheet_list`. The
  full URL is accepted, Drive file IDs are not. The Drive `path` of the `.osheet`
  works too, and so does a `name` stored with `spreadsheet_register`
  (`spreadsheet_create` registers the files it creates).
- `spreadsheet_read_sheet` returns formula results; `include_formulas: true`
  returns the formulas as typed. Dates are serial day numbers.
- `spreadsheet_create` cannot pick a folder: the Spreadsheet API decides where
  the file goes; move it with `drive_move_file`.

## Privacy

The server and the extension send nothing to third parties. The extension only
talks to the server URL you enter; the server only talks to your own DSM and to
the Spreadsheet container on the NAS. Files and spreadsheet contents go to your AI
assistant only when a tool returns them.

## Advanced

### How it runs

- Streamable HTTP transport on `/mcp`, listening on loopback (`127.0.0.1:3100`).
- The DSM reverse proxy terminates HTTPS and forwards to it.
- Clients authenticate with a static bearer token (`MCP_AUTH_TOKEN`).
- `GET /health` is unauthenticated and only returns `{"status": "ok"}`.
- Host header validation: only loopback and `MCP_ALLOWED_HOSTS` (or the address
  entered in `/setup`) are accepted (other hosts get `421`); browser requests
  from foreign origins get `403`.

### Configuration

Each setting comes, in this order of precedence, from an environment variable,
from the files written by the setup page in `DATA_DIR` (`config.json`, and
`secrets.json` readable by the server only), or from its default. The
variables are listed in [`deploy/.env.example`](deploy/.env.example). Settings
are validated at startup: an invalid value stops the server with a message
naming it. When only required values are missing (or with `SETUP_MODE=true`),
the server serves the setup page instead of `/mcp`.

### Install on a Synology NAS

The [Container Manager guide](docs/install-container-manager.md) needs no
terminal. To install over SSH instead, with every setting in a `.env` file:

```bash
mkdir -p /volume1/docker/syno-workspace-mcp && cd /volume1/docker/syno-workspace-mcp
curl -fsSLO https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/main/deploy/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/alexcane/syno-workspace-mcp/main/deploy/.env.example
vi .env   # fill in DSM URL, account, secrets (openssl rand -hex 32), allowed hosts
chmod 600 .env
mkdir -p data && sudo chown 1000:1000 data
sudo /usr/local/bin/docker compose pull && sudo /usr/local/bin/docker compose up -d   # docker is not on sudo's PATH on DSM
curl -fsS http://127.0.0.1:3100/health
```

The DSM account needs Synology Drive and read/write access to its team folders,
without two-factor authentication. Then add a DSM reverse proxy rule
(*Control Panel → Login Portal → Advanced → Reverse Proxy*): source HTTPS
`<host>:<port>`, destination `http://localhost:3100`, HTTP 1.1,
connection/send/read timeouts 3600 s. Add `<host>` to `MCP_ALLOWED_HOSTS`.

## Development

Building, testing and releasing: see [docs/development.md](docs/development.md).

## License

MIT, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Python port of
[vocweb/synology-mcp-server](https://github.com/vocweb/synology-mcp-server),
built with [FastMCP](https://gofastmcp.com).
