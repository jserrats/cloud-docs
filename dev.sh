#!/usr/bin/env bash
# Local debug server: ./dev.sh <path to a cloud checkout> [port]
#
# Rebuilds the map whenever the checkout changes (a commit or an edit), and the
# UI bundle whenever web/ changes. Open pages reload on their own.
set -euo pipefail

repo=$(cd "${1:?usage: ./dev.sh <path to a cloud checkout> [port]}" && pwd)
port=${2:-8080}
here=$(cd "$(dirname "$0")" && pwd)
data=$(mktemp -d)

cd "$here/web"
[ -d node_modules ] || npm ci
mkdir -p dist
# Served straight from web/, so HTML and CSS edits need no rebuild.
ln -sf ../index.html dist/index.html
ln -sf ../style.css dist/style.css
npx esbuild src/main.js --bundle --sourcemap --target=es2022 --outfile=dist/app.js --watch=forever &
esbuild_pid=$!
trap 'kill $esbuild_pid 2>/dev/null; rm -rf "$data"' EXIT

cd "$here"
echo "map of $repo on http://localhost:$port"
REPO_LINK="$repo" DATA_DIR="$data" WEB_DIR="$here/web/dist" PORT="$port" POLL_SECONDS=2 \
  uv run python -m server.serve
