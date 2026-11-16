# syno-workspace-mcp: installation instructions for AI agents

These instructions are for an AI agent. Follow them in order. Do not skip a step
and do not guess a value: when something is missing or unclear, stop and ask
the user.

## Rules

Apply these rules during the whole installation:

1. Never run `sudo` (or an elevated PowerShell) without asking the user first.
   No step below needs it.
2. Never overwrite an existing configuration file. Read it, merge your change,
   show the user the diff, and write it only after the user agrees.
3. Never write the access token to a file tracked by version control (a
   repository, a dotfiles repository). Never print it in clear text in a message
   or a summary: write `***` instead.
4. Never install a missing prerequisite yourself. Stop and tell the user what is
   missing.
5. Never invent a value or use an example value (`nas.example.com`,
   `<access token>`): ask the user.
6. At the end, summarize what you did, the files you changed, and what the user
   still has to do by hand.

Names in capitals in the commands below (`SERVER_URL`, `HEALTH_URL`, `TOKEN`,
`MCPB_URL`, `MCPB_FILE`) are placeholders: replace each one with its real value
before running the command.

## 1. Goal

Connect the user's Claude Code and/or Claude Desktop to a syno-workspace-mcp
server that already runs on their Synology NAS, then prove that the connection
works.

You do not install the server itself: that is done by the user in the DSM web
interface, following
https://github.com/alexcane/syno-workspace-mcp/blob/main/docs/install-container-manager.md
(Container Manager, then the `/setup` page, which needs a DSM administrator
password that the user must type themselves). If the server is not installed
yet, stop and give the user that link.

## 2. Check the prerequisites

Detect the operating system first:

- macOS or Linux: run `uname -s`. `Darwin` means macOS, `Linux` means Linux.
- Windows: in PowerShell, `$env:OS` returns `Windows_NT`.

Use `curl` on macOS and Linux. On Windows, use `curl.exe` (in PowerShell, plain
`curl` is an alias of `Invoke-WebRequest` and takes other arguments).

Ask the user which clients to configure: Claude Code, Claude Desktop, or both.
Then run the checks for those clients:

| Client | Check | Expected |
| --- | --- | --- |
| Claude Code | `claude --version` | A version number |
| Claude Desktop, macOS | `ls -d /Applications/Claude.app` | `/Applications/Claude.app` |
| Claude Desktop, Windows | Ask the user to confirm that Claude Desktop is installed | Yes |
| Claude Desktop, Linux | None | Not supported: the extension only exists for macOS and Windows. Tell the user and do not configure Claude Desktop. |
| Any | `curl --version` (`curl.exe --version` on Windows) | A version number |

If a check fails, stop and tell the user which prerequisite is missing.

## 3. Ask the user for this information

Ask for all of it before going further (you already know which clients to
configure from step 2):

1. **Is the server already installed and set up on the NAS** (the `/setup` page
   was completed)? If not, stop: see step 1.
2. **Server URL**: the full address shown at the end of the `/setup` page. It
   starts with `https://` and ends with `/mcp`, for example
   `https://<host>:<port>/mcp`. Call it `SERVER_URL` below. The health address
   `HEALTH_URL` is the same URL with `/mcp` replaced by `/health`.
3. **Access token**: shown once at the end of the `/setup` page. Call it
   `TOKEN` below. If the user lost it, they must run the setup again (see
   *Run the setup again* in the guide linked in step 1): stop.

Check the format before going further: `SERVER_URL` must start with `https://`
(or `http://localhost` / `http://127.0.0.1`) and end with `/mcp`; `TOKEN` must
be at least 32 characters long. If not, ask again.

## 4. Check that the server answers

Run these checks before configuring any client. Replace `HEALTH_URL`,
`SERVER_URL` and `TOKEN` with the user's values.

4.1. Health check, no token needed:

```bash
curl -sS HEALTH_URL
```

On Windows: `curl.exe -sS HEALTH_URL`.

Expected output: `{"status":"ok"}`. If you get `{"status":"setup"}`, the server
is still waiting for its `/setup` page: stop and tell the user to finish it.
For any other result, see step 8.

4.2. Without a token, `/mcp` must refuse the request:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST SERVER_URL
```

On Windows: `curl.exe -sS -o NUL -w "%{http_code}" -X POST SERVER_URL`.

Expected output: `401`.

4.3. With the token, the server must accept an MCP `initialize` request.

macOS and Linux:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST SERVER_URL \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install-check","version":"1"}}}'
```

Windows (PowerShell; the body goes through a file because PowerShell may strip
the quotes of a JSON argument):

```powershell
Set-Content -Path "$env:TEMP\mcp-init.json" -NoNewline -Encoding ascii -Value '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install-check","version":"1"}}}'
curl.exe -sS -o NUL -w "%{http_code}" -X POST SERVER_URL -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" --data "@$env:TEMP\mcp-init.json"
Remove-Item "$env:TEMP\mcp-init.json"
```

Expected output: `200`. A `401` means the token is wrong: ask the user to check
it. Do not go further until you get `200`.

## 5. Configure Claude Code

Skip this step if the user did not ask for Claude Code.

5.1. Check whether a server named `synology` already exists:

```bash
claude mcp list
```

If a line starts with `synology:`, an entry already exists. Ask the user
whether to replace it. Only if they agree, remove it first:

```bash
claude mcp remove synology --scope user
```

5.2. Add the server for the user (all projects):

```bash
claude mcp add --scope user --transport http synology SERVER_URL --header "Authorization: Bearer TOKEN"
```

Claude Code stores this entry in its own user configuration (`~/.claude.json`),
not in a project. Do not edit that file by hand and do not use
`--scope project`: it writes `.mcp.json` in the current repository, which may be
committed with the token.

5.3. Check the connection:

```bash
claude mcp list
```

Expected: a line `synology: SERVER_URL (HTTP) - ✔ Connected`. If it shows
`✘ Failed to connect`, see step 8.

## 6. Configure Claude Desktop

Skip this step if the user did not ask for Claude Desktop. Claude Desktop uses
the syno-workspace-mcp extension (`.mcpb` file). The user types the URL and the
token into Claude Desktop themselves; you only download and open the file. You
do not need to edit `claude_desktop_config.json`.

6.1. Find the download address of the latest extension. Tell the user the file
name and its source (the GitHub releases of `alexcane/syno-workspace-mcp`)
before downloading it.

macOS:

```bash
curl -fsSL https://api.github.com/repos/alexcane/syno-workspace-mcp/releases/latest \
  | grep -o '"browser_download_url": *"[^"]*\.mcpb"' | cut -d'"' -f4
```

Windows (PowerShell):

```powershell
(Invoke-RestMethod https://api.github.com/repos/alexcane/syno-workspace-mcp/releases/latest).assets | Where-Object name -like '*.mcpb' | Select-Object -First 1 -ExpandProperty browser_download_url
```

The address ends with `syno-workspace-mcp-<version>.mcpb`. Call it `MCPB_URL`
and the file name `MCPB_FILE`.

6.2. Download it to the user's Downloads folder and open it with Claude
Desktop.

macOS:

```bash
curl -fsSL -o ~/Downloads/MCPB_FILE MCPB_URL
open -a Claude ~/Downloads/MCPB_FILE
```

Windows (PowerShell):

```powershell
curl.exe -fsSL -o "$env:USERPROFILE\Downloads\MCPB_FILE" MCPB_URL
Start-Process "$env:USERPROFILE\Downloads\MCPB_FILE"
```

If the file does not open in Claude Desktop, tell the user to drag it onto
*Settings → Extensions* in Claude Desktop.

6.3. Tell the user to finish in Claude Desktop:

1. Accept the warning: the extension is not signed yet, so Claude Desktop says
   the developer is not verified. Click **Install**.
2. Enter **Server URL** = `SERVER_URL` and **Access token** = the token from
   the `/setup` page (Claude Desktop masks this field and stores it securely).
3. Make sure the extension is enabled.
4. On macOS, if asked to let Claude access the local network, allow it.

You cannot check the result from the command line: go to step 7.

## 7. Verify

7.1. Claude Code: run a one-off, non-interactive Claude Code session that may
only call the listing tool (it reads, it changes nothing):

```bash
claude -p "Use the synology MCP server: call drive_list_files on /team-folders and reply with only the folder names." --allowedTools mcp__synology__drive_list_files
```

Expected: the names of the Drive team folders the server's DSM account can open
(for example `workspace`), one per line. An answer saying that the tool is not
available means the server is not connected: see step 5.3 and step 8.

7.2. Claude Desktop: ask the user to start a **new** conversation (a
conversation opened before the configuration does not see the new tools) and to
send:

```text
Is the Synology MCP server set up correctly? List my Drive team folders.
```

Expected: Claude calls the `drive_list_files` tool (it may ask the user to
allow it first) and lists the team folders. Ask the user what they see.

## 8. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `421` (`Misdirected Request`) on `/health` or `/mcp` | The host name in `SERVER_URL` is not the one entered in `/setup` | Use the exact host name shown at the end of `/setup`, or run the setup again with this host name. |
| `502 Bad Gateway` | The `syno-workspace-mcp` container is not running on the NAS | Ask the user to start the project in *Container Manager → Project*, then check its log. A log starting with `Invalid configuration:` names the wrong values. |
| Connection refused, or timeout | The DSM reverse proxy rule is missing or uses another port, or this computer is not on the NAS network / VPN | Ask the user to check the rule in *Control Panel → Login Portal → Advanced → Reverse Proxy* and their network or VPN. |
| `401` with the token | Wrong or old token | Ask the user to check the token. If it is lost, they must run the setup again and you must update every client. |
| Certificate error (`SSL certificate problem`, `unable to verify`) | The NAS certificate does not cover the host name or comes from a private CA | Ask the user to use a host name covered by a valid certificate (for example Let's Encrypt in DSM). Do not disable certificate checks. |
| Claude Desktop: `EHOSTUNREACH`, or the tools never appear | macOS did not allow Claude to access the local network, or the extension is disabled | Ask the user to allow Claude in *System Settings → Privacy & Security → Local Network* and to enable the extension in *Settings → Extensions*. |

## 9. Report to the user

End with a short summary:

- what you checked and its result (`/health`, `401`, `200`, step 7);
- the clients you configured, and how (command run, file downloaded);
- the files you created or changed (for example `~/Downloads/MCPB_FILE`;
  Claude Code changes `~/.claude.json` itself);
- what the user still has to do: finish the Claude Desktop extension (step 6.3)
  and run its test prompt (step 7.2), if not done yet;
- never the token: write `***`.
