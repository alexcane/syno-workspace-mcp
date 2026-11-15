// Relays MCP messages between a local stdio client (Claude Desktop) and a
// remote Streamable HTTP server that expects a static Bearer token.

import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

export class ConfigError extends Error {}

/**
 * Reads and validates the bridge settings from the environment.
 * @param {Record<string, string | undefined>} env
 * @returns {{ url: URL, token: string }}
 */
export function parseConfig(env) {
  const rawUrl = (env.MCP_URL ?? "").trim();
  if (!rawUrl) {
    throw new ConfigError("Server URL is missing: set it in the extension settings.");
  }
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new ConfigError(`Server URL is not a valid URL: ${rawUrl}`);
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname))) {
    throw new ConfigError("Server URL must use https:// (plain http is only allowed on localhost).");
  }
  if (url.username || url.password) {
    throw new ConfigError("Server URL must not contain credentials.");
  }
  if (url.pathname === "" || url.pathname === "/") {
    url.pathname = "/mcp";
  }

  let token = (env.MCP_TOKEN ?? "").trim();
  if (/^bearer\s+/i.test(token)) {
    token = token.replace(/^bearer\s+/i, "");
  }
  if (!token) {
    throw new ConfigError("Access token is missing: set it in the extension settings.");
  }
  if (/[\s\r\n]/.test(token)) {
    throw new ConfigError("Access token must not contain spaces or line breaks.");
  }
  return { url, token };
}

/**
 * Builds the HTTP transport towards the remote server.
 * @param {{ url: URL, token: string }} config
 * @param {typeof fetch} [fetchImpl]
 */
export function createRemoteTransport({ url, token }, fetchImpl) {
  return new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { Authorization: `Bearer ${token}` } },
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  });
}

/**
 * Explains a transport error without echoing secrets.
 * @param {unknown} error
 */
export function describeError(error) {
  const code = /** @type {{ code?: unknown }} */ (error)?.code;
  if (code === 401) {
    return "The server rejected the access token (401). Check the token in the extension settings.";
  }
  if (code === 421) {
    return "The server does not accept this host name (421). Add it to MCP_ALLOWED_HOSTS on the server.";
  }
  if (code === 404) {
    return "Not found (404): either the URL is wrong (it usually ends with /mcp) or the server no longer knows this session (it was restarted). Check the URL, then restart Claude.";
  }
  const cause = /** @type {{ cause?: { code?: string, message?: string } }} */ (error)?.cause;
  const reason = cause?.code ?? cause?.message;
  if (reason) {
    return `Cannot reach the server (${reason}). Check the URL and that you are on the right network or VPN.`;
  }
  return error instanceof Error ? error.message : String(error);
}

/**
 * Wires two transports together and starts them.
 * Each `initialize` opens a fresh HTTP transport, so a re-initialization or a
 * retry after a failed handshake never reuses a stale `mcp-session-id`. Before
 * the handshake succeeds, other messages are answered locally instead of being
 * sent: the server would reply 400 with an unregistered session ID that the SDK
 * transport adopts, turning the following `initialize` into a 404.
 *
 * @param {import("@modelcontextprotocol/sdk/shared/transport.js").Transport} local
 * @param {() => StreamableHTTPClientTransport} createRemote
 * @param {{ log?: (line: string) => void }} [options]
 * @returns {Promise<{ closed: Promise<void> }>}
 */
export async function startBridge(local, createRemote, { log = (line) => process.stderr.write(`${line}\n`) } = {}) {
  const report = (/** @type {unknown} */ error) => log(`syno-workspace-mcp: ${describeError(error)}`);
  /** @type {StreamableHTTPClientTransport | null} */
  let remote = null;
  let initialized = false;
  let closing = false;
  /** @type {() => void} */
  let resolveClosed = () => {};
  const closed = new Promise((resolve) => {
    resolveClosed = resolve;
  });

  const shutdown = async () => {
    if (closing) return;
    closing = true;
    await Promise.allSettled([remote?.close(), local.close()]);
    resolveClosed();
  };

  /** @param {StreamableHTTPClientTransport} transport */
  const retire = (transport) => {
    transport.onclose = undefined;
    transport.onmessage = undefined;
    transport.onerror = undefined;
    transport.close().catch(() => {});
  };

  /** @param {string | number} initId */
  const openRemote = async (initId) => {
    const transport = createRemote();
    transport.onmessage = (message) => {
      if (transport !== remote) return;
      if ("id" in message && message.id === initId) {
        if ("result" in message) {
          initialized = true;
          const version = message.result?.protocolVersion;
          if (typeof version === "string") transport.setProtocolVersion(version);
        }
      }
      local.send(message).catch(report);
    };
    transport.onerror = report;
    transport.onclose = () => {
      if (transport === remote) void shutdown();
    };
    await transport.start();
    return transport;
  };

  /**
   * @param {string | number} id
   * @param {number} code
   * @param {string} text
   */
  const replyError = (id, code, text) =>
    local.send({ jsonrpc: "2.0", id, error: { code, message: text } }).catch(() => {});

  local.onmessage = async (message) => {
    const isRequest = "method" in message && "id" in message && message.id !== undefined;
    const isInit = isRequest && message.method === "initialize";

    if (isInit) {
      if (remote) retire(remote);
      initialized = false;
      remote = null;
      try {
        remote = await openRemote(message.id);
      } catch (error) {
        report(error);
        replyError(message.id, -32000, describeError(error));
        return;
      }
    } else if (!initialized || !remote) {
      // Answer like a server that has not been initialized yet; `server/discover`
      // probes then fall back to `initialize`.
      if (isRequest) {
        const unknown = message.method === "server/discover";
        replyError(message.id, unknown ? -32601 : -32600, unknown ? "Method not found" : "Server not initialized");
      }
      return;
    }

    const target = remote;
    target.send(message).catch((error) => {
      report(error);
      if (isInit && target === remote) {
        retire(target);
        remote = null;
      }
      if (isRequest) replyError(message.id, -32000, describeError(error));
    });
  };

  local.onerror = report;
  local.onclose = () => void shutdown();
  await local.start();
  return { closed };
}
