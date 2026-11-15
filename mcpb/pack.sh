#!/bin/sh
# Builds dist/syno-workspace-mcp-<version>.mcpb with production dependencies only.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cd "$here"
version="$(node -p 'require("./manifest.json").version')"
if [ "$version" != "$(node -p 'require("./package.json").version')" ]; then
  echo "manifest.json and package.json versions differ" >&2
  exit 1
fi

cp -R manifest.json package.json package-lock.json server "$stage/"
cp ../LICENSE ../NOTICE "$stage/"
(cd "$stage" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund)

mkdir -p dist
out="dist/syno-workspace-mcp-${version}.mcpb"
rm -f "$out"
node_modules/.bin/mcpb validate "$stage/manifest.json"
node_modules/.bin/mcpb pack "$stage" "$out"
echo "$out"
