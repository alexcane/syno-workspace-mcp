# Development

## Server

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

## Claude Desktop extension

The extension lives in [`mcpb/`](../mcpb) (Node.js ≥ 18):

```bash
cd mcpb && npm ci
npm test       # node:test, against an in-process MCP server
npm run pack   # dist/syno-workspace-mcp-<version>.mcpb, production deps only
```

## Releases

Pushing a tag `vX.Y.Z` matching `version` in `pyproject.toml` builds the
`linux/amd64` image and publishes `ghcr.io/alexcane/syno-workspace-mcp:X.Y.Z`.
No `latest` tag is published. The same tag attaches the `.mcpb` extension to the
GitHub release; `version` in `mcpb/manifest.json` and `mcpb/package.json` must
match too.
