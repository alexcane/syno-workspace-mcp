# syno-workspace-mcp

MCP server for **Synology Drive** and **Synology Spreadsheet**, built with
[FastMCP](https://gofastmcp.com). Python port of
[vocweb/synology-mcp-server](https://github.com/vocweb/synology-mcp-server).

> **Status: milestone 3 (Drive).** The 11 Drive tools are available; write tools
> that can overwrite or delete (`drive_move_file`, `drive_delete_file`) do nothing
> unless called with `confirm: true`. Spreadsheet tools land in the next milestone.

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
