import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import { after, before, describe, it } from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";

import { ConfigError, createRemoteTransport, describeError, parseConfig, startBridge } from "../server/bridge.js";

const TOKEN = "test-token-0123456789";

describe("parseConfig", () => {
  it("accepts an https URL and a token", () => {
    const { url, token } = parseConfig({ MCP_URL: " https://nas.example.com:3443/mcp ", MCP_TOKEN: ` ${TOKEN} ` });
    assert.equal(url.href, "https://nas.example.com:3443/mcp");
    assert.equal(token, TOKEN);
  });

  it("appends /mcp to a bare host URL", () => {
    assert.equal(parseConfig({ MCP_URL: "https://nas.example.com:3443", MCP_TOKEN: TOKEN }).url.pathname, "/mcp");
  });

  it("strips a pasted Bearer prefix", () => {
    assert.equal(parseConfig({ MCP_URL: "https://nas.example.com", MCP_TOKEN: `Bearer ${TOKEN}` }).token, TOKEN);
  });

  it("allows plain http on loopback only", () => {
    assert.ok(parseConfig({ MCP_URL: "http://127.0.0.1:3100/mcp", MCP_TOKEN: TOKEN }));
    assert.ok(parseConfig({ MCP_URL: "http://localhost:3100/mcp", MCP_TOKEN: TOKEN }));
    assert.throws(() => parseConfig({ MCP_URL: "http://nas.example.com/mcp", MCP_TOKEN: TOKEN }), ConfigError);
  });

  for (const [label, env] of [
    ["missing URL", { MCP_TOKEN: TOKEN }],
    ["invalid URL", { MCP_URL: "nas.example.com", MCP_TOKEN: TOKEN }],
    ["credentials in URL", { MCP_URL: "https://u:p@nas.example.com/mcp", MCP_TOKEN: TOKEN }],
    ["missing token", { MCP_URL: "https://nas.example.com/mcp" }],
    ["unsubstituted placeholder", { MCP_URL: "https://nas.example.com/mcp", MCP_TOKEN: "${user_config.x} y" }],
    ["token with a line break", { MCP_URL: "https://nas.example.com/mcp", MCP_TOKEN: "abc\ndef" }],
  ]) {
    it(`rejects ${label}`, () => {
      assert.throws(() => parseConfig(env), ConfigError);
    });
  }

  it("never echoes the token in error messages", () => {
    try {
      parseConfig({ MCP_URL: "ftp://nas.example.com", MCP_TOKEN: TOKEN });
      assert.fail("expected a ConfigError");
    } catch (error) {
      assert.ok(!String(error.message).includes(TOKEN));
    }
  });
});

describe("describeError", () => {
  it("explains HTTP status codes", () => {
    assert.match(describeError({ code: 401 }), /access token/);
    assert.match(describeError({ code: 421 }), /MCP_ALLOWED_HOSTS/);
    assert.match(describeError({ code: 404 }), /\/mcp/);
  });

  it("explains network errors", () => {
    assert.match(describeError(Object.assign(new Error("fetch failed"), { cause: { code: "ECONNREFUSED" } })), /ECONNREFUSED/);
  });
});

describe("bridge", () => {
  /** @type {import("node:http").Server} */
  let httpServer;
  /** @type {URL} */
  let url;
  /** @type {string[]} */
  const seenAuth = [];
  /** @type {Map<string, StreamableHTTPServerTransport>} */
  const sessions = new Map();
  /** @type {{ method?: string, session: boolean }[]} */
  const posted = [];

  before(async () => {
    httpServer = createServer(async (req, res) => {
      seenAuth.push(req.headers.authorization ?? "");
      if (req.headers.authorization !== `Bearer ${TOKEN}`) {
        res.writeHead(401, { "content-type": "application/json" }).end('{"error":"invalid_token"}');
        return;
      }
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;

      const sessionId = req.headers["mcp-session-id"];
      posted.push({ method: body?.method, session: typeof sessionId === "string" });
      if (req.method === "POST" && sessionId === undefined && body?.method !== "initialize") {
        // FastMCP answers with an unregistered session ID here.
        res
          .writeHead(400, { "content-type": "application/json", "mcp-session-id": randomUUID() })
          .end('{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Bad Request: Missing session ID"}}');
        return;
      }
      if (typeof sessionId === "string" && !sessions.has(sessionId)) {
        res.writeHead(404, { "content-type": "application/json" }).end('{"error":"Session not found"}');
        return;
      }
      let transport = typeof sessionId === "string" ? sessions.get(sessionId) : undefined;
      if (!transport) {
        transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          onsessioninitialized: (id) => sessions.set(id, transport),
        });
        const server = new McpServer({ name: "fake-syno", version: "0.0.0" });
        server.registerTool("drive_list_files", { description: "List files" }, async () => ({
          content: [{ type: "text", text: "labs" }],
        }));
        await server.connect(transport);
      }
      await transport.handleRequest(req, res, body);
    });
    await new Promise((resolve) => httpServer.listen(0, "127.0.0.1", resolve));
    const address = /** @type {import("node:net").AddressInfo} */ (httpServer.address());
    url = new URL(`http://127.0.0.1:${address.port}/mcp`);
  });

  after(async () => {
    await Promise.allSettled([...sessions.values()].map((t) => t.close()));
    httpServer.closeAllConnections();
    await new Promise((resolve) => httpServer.close(resolve));
  });

  /** @param {string} token */
  async function connect(token) {
    const [clientSide, bridgeSide] = InMemoryTransport.createLinkedPair();
    /** @type {string[]} */
    const logs = [];
    const bridge = await startBridge(bridgeSide, () => createRemoteTransport({ url, token }), {
      log: (line) => logs.push(line),
    });
    const client = new Client({ name: "test-desktop", version: "0.0.0" });
    return { client, clientSide, bridge, logs };
  }

  it("relays initialize, tools/list and tools/call with the Bearer token", async () => {
    const { client, clientSide, bridge } = await connect(TOKEN);
    await client.connect(clientSide);

    const { tools } = await client.listTools();
    assert.deepEqual(
      tools.map((t) => t.name),
      ["drive_list_files"],
    );
    const result = await client.callTool({ name: "drive_list_files", arguments: {} });
    assert.deepEqual(result.content, [{ type: "text", text: "labs" }]);
    assert.ok(seenAuth.length > 0 && seenAuth.every((h) => h === `Bearer ${TOKEN}`));

    await client.close();
    await bridge.closed;
  });

  it("turns a 401 into a JSON-RPC error and a readable log line", async () => {
    const { client, clientSide, logs } = await connect("wrong-token");
    await assert.rejects(client.connect(clientSide), /access token/);
    assert.ok(logs.some((line) => line.includes("401")));
    assert.ok(logs.every((line) => !line.includes("wrong-token")));
    await client.close();
  });

  it("answers a server/discover probe locally, then initializes cleanly", async () => {
    const { client, clientSide, bridge } = await connect(TOKEN);
    const [probeSide, probeBridgeSide] = InMemoryTransport.createLinkedPair();
    await startBridge(probeBridgeSide, () => createRemoteTransport({ url, token: TOKEN }), { log: () => {} });
    /** @type {unknown[]} */
    const replies = [];
    probeSide.onmessage = (m) => replies.push(m);
    await probeSide.start();
    const before = posted.length;
    await probeSide.send({ jsonrpc: "2.0", id: 7, method: "server/discover", params: {} });
    await probeSide.send({ jsonrpc: "2.0", id: 8, method: "tools/list", params: {} });
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(replies, [
      { jsonrpc: "2.0", id: 7, error: { code: -32601, message: "Method not found" } },
      { jsonrpc: "2.0", id: 8, error: { code: -32600, message: "Server not initialized" } },
    ]);
    assert.equal(posted.length, before, "nothing is sent before initialize");
    await probeSide.close();

    await client.connect(clientSide);
    assert.equal((await client.listTools()).tools.length, 1);
    await client.close();
    await bridge.closed;
  });

  it("opens a new session when the client initializes again", async () => {
    const [clientSide, bridgeSide] = InMemoryTransport.createLinkedPair();
    const bridge = await startBridge(bridgeSide, () => createRemoteTransport({ url, token: TOKEN }), { log: () => {} });
    const first = new Client({ name: "a", version: "0" });
    await first.connect(clientSide);
    await first.listTools();
    // A second client reuses the same bridge process and transport pair.
    clientSide.onmessage = undefined;
    const second = new Client({ name: "b", version: "0" });
    const before = posted.length;
    await second.connect({
      start: async () => {},
      send: (m) => clientSide.send(m),
      close: async () => clientSide.close(),
      set onmessage(h) {
        clientSide.onmessage = h;
      },
    });
    assert.equal((await second.listTools()).tools.length, 1);
    const init = posted.slice(before).find((p) => p.method === "initialize");
    assert.equal(init?.session, false, "the second initialize carries no stale session ID");
    await second.close();
    await bridge.closed;
  });
});
