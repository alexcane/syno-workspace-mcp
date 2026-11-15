#!/usr/bin/env node
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ConfigError, createRemoteTransport, parseConfig, startBridge } from "./bridge.js";

let config;
try {
  config = parseConfig(process.env);
} catch (error) {
  if (error instanceof ConfigError) {
    process.stderr.write(`syno-workspace-mcp: ${error.message}\n`);
    process.exit(1);
  }
  throw error;
}

const { closed } = await startBridge(new StdioServerTransport(), () => createRemoteTransport(config));
process.stdin.on("end", () => process.exit(0));
await closed;
process.exit(0);
