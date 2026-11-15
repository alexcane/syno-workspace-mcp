# Install with Container Manager (no SSH)

This guide installs syno-workspace-mcp on a Synology NAS using only the DSM web
interface, then connects Claude Desktop. Allow about 20 minutes.

For a command-line install over SSH, see the [README](../README.md#install-on-a-synology-nas).

## Before you start

You need:

- DSM 7.2 or later, an administrator account, and these packages from
  *Package Center*: **Container Manager**, **Synology Drive Server** and
  **Synology Office** (Office is only needed for spreadsheets).
- An HTTPS address for your NAS with a valid certificate, for example
  `nas.example.com` with a Let's Encrypt certificate
  (*Control Panel → Security → Certificate*). Claude will connect to it.
- A way to generate random passwords: a password manager, or the *Passwords*
  app on a Mac (see step 3).

DSM may show these screens in your own language: the labels below are the
English ones.

The server is meant to be reached from your local network or through a VPN.
Exposing it to the Internet is possible but not covered here: the access token
would then be the only protection.

## 1. Create a dedicated DSM account

The server signs in to DSM with its own account, limited to Drive and Office.

1. Open *Control Panel → User & Group → User* and click **Create**.
2. *Enter user information*:
   - **Name**: `mcp_service`
   - **Password**: generate one with **Generate Random Password** or your
     password manager, and keep it: you will paste it in step 4.
   - Tick **Disallow the user to change account password**.
3. *Join groups*: keep the default `users` group.
4. *Assign shared folders permissions*: tick **Read/Write** on each Drive team
   folder the server should use (for example `labs`). Leave the other rows
   unticked. Do not tick **No access** on a folder the server needs: it
   overrides everything else.
5. *Assign user quota*: leave as is.
6. *Assign application permissions*: **Allow** *Synology Drive* and
   *Synology Office*, **Deny** everything else.
7. *Set user speed limit*: leave as is, then **Apply** on *Confirm settings*.

Do not enable two-factor authentication on this account: the server could no
longer sign in.

## 2. Create the project folder

1. Open *File Station* and go to the `docker` shared folder (Container Manager
   creates it when installed).
2. Create a folder `syno-workspace-mcp`, and inside it a folder `data`.
3. Right-click `data` → **Properties** → **Permission** tab → **Create**:
   - **User or group**: `Everyone`
   - **Type**: Allow
   - **Apply to**: This folder, Child folders, Child files
   - **Permission**: tick **Read** and **Write**
4. Click **Done**, then **Save**.

Why: the server runs in its container as a user that has no DSM account, so it
needs this permission to save its data (the list of spreadsheet names you
register). Nothing secret is stored in `data`.

## 3. Prepare the values

Write these five values in a note before creating the project, then copy and
paste them: retyping them is the most common source of mistakes.

| Placeholder | What to put |
| --- | --- |
| `CHANGE_ME_DSM_URL` | The address you use to open DSM over HTTPS, with its port, for example `https://nas.example.com:5001` |
| `CHANGE_ME_DSM_PASSWORD` | The `mcp_service` password from step 1 |
| `CHANGE_ME_SPREADSHEET_SECRET` | A random secret, any length (40 characters is fine) |
| `CHANGE_ME_ACCESS_TOKEN` | Another random secret, **at least 32 characters**: Claude will use it to connect |
| `CHANGE_ME_HOST_NAME` | The host name Claude will use, without `https://` or port, for example `nas.example.com` |

The two secrets may only contain letters, digits and hyphens. To generate them:

- **Password manager**: create a 48-character password with symbols turned off.
- **Passwords app on a Mac**: create a new password and copy the suggested
  strong password (for example `abcdef-ghijkl-mnopqr`). It has 20 characters:
  for the access token, paste two suggestions one after the other.

If the DSM password contains a `$`, write it twice (`$$`).

## 4. Create the project

1. Open *Container Manager → Project* and click **Create**.
2. **Project Name**: `syno-workspace-mcp`.
3. **Path**: select `/docker/syno-workspace-mcp`.
4. **Source**: choose **Create docker-compose.yml**, then paste the content of
   [`deploy/container-manager/docker-compose.yml`](../deploy/container-manager/docker-compose.yml).
5. Replace each of the five `CHANGE_ME_…` values with the matching value from
   step 3, keeping the double quotes. Check that no `CHANGE_ME` is left and
   that each value is on the right line.
6. Click **Next**. Leave the Web Station portal disabled.
7. Check the summary, tick the option to start the project once it is created,
   and click **Done**.

Container Manager downloads the two images and starts two containers:
`synology-spreadsheet-api` and `syno-workspace-mcp`.

## 5. Check that it runs

1. Open *Container Manager → Container*. Both containers should be running
   (green dot).
2. Open `syno-workspace-mcp`. After a minute its status shows **Healthy**. In
   the **Log** tab, you should see a line containing
   `Uvicorn running on http://127.0.0.1:3100`.

If the container stops and its log starts with `Invalid configuration:`, the
next lines name each wrong value, for example
`MCP_ALLOWED_HOSTS: Value error, still holds a CHANGE_ME placeholder`. Fix them
as described in [Change a value](#change-a-value).

## 6. Add a reverse proxy rule

The server only listens inside the NAS. DSM's reverse proxy publishes it over
HTTPS.

1. Open *Control Panel → Login Portal → Advanced → Reverse Proxy* and click
   **Create**.
2. **Reverse Proxy Name**: `syno-workspace-mcp`.
3. **Source**: Protocol `HTTPS`, Hostname `nas.example.com` (your host name),
   Port `3443` (any free port).
4. **Destination**: Protocol `HTTP`, Hostname `localhost`, Port `3100`.
5. **Advanced Settings**: set **Proxy send timeout** and **Proxy read
   timeout** to `3600` (Claude keeps a connection open), and **Proxy HTTP
   version** to `HTTP 1.1`.
6. Click **Save**, and check that the rule now appears in the list.

Make sure the certificate covering your host name is used for this rule
(*Control Panel → Security → Certificate → Settings*).

## 7. Test the address

Open `https://nas.example.com:3443/health` in a browser (with your host name and
port). The page should show `{"status":"ok"}`.

| You see | Likely cause |
| --- | --- |
| `421 Misdirected Request` | `MCP_ALLOWED_HOSTS` does not match the host name in the address |
| `502 Bad Gateway` | The `syno-workspace-mcp` container is not running (step 5) |
| Connection refused | The reverse proxy rule was not saved, or uses another port (step 6) |
| Other connection error | The computer is not on the NAS network / VPN |

## 8. Connect Claude

Your server URL is `https://nas.example.com:3443/mcp` and the access token is
the `MCP_AUTH_TOKEN` value.

- **Claude Desktop**: install the extension, see
  [Claude Desktop extension](../README.md#claude-desktop-extension).
- **Claude Code**: see [Connect a client](../README.md#connect-a-client).

## Change a value

1. Open *Container Manager → Project → syno-workspace-mcp → YAML
   Configurations* and edit the value.
2. Save. Container Manager offers to rebuild the project: accept (**Build**).
3. Click **Start** if the project does not start by itself.

Saving alone does not apply the change: the containers keep running with the
old values until the project is rebuilt.

## Update to a new version

Change the version at the end of `image: ghcr.io/alexcane/syno-workspace-mcp:…`
to the new one from the
[releases page](https://github.com/alexcane/syno-workspace-mcp/releases), as in
[Change a value](#change-a-value). Container Manager downloads the new image.
Your values and the `data` folder are kept.

## Security notes

- Container Manager stores the project in `/docker/syno-workspace-mcp/compose.yaml`,
  which holds the DSM password and the access token. By default every DSM user
  may be able to read the `docker` shared folder: check its permissions in
  *Control Panel → Shared Folder → docker → Edit → Permissions*. Never tick
  **No access** for the `users` group: every account belongs to it, so that
  would lock out administrators too.
- Anyone with the access token can read, change and delete files the
  `mcp_service` account can reach. If it leaks, replace it in *YAML
  Configurations*, save, and update it in Claude.
- Tools that overwrite, rename or delete do nothing unless Claude calls them
  with `confirm: true`.
