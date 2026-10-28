"""Serve the map, and rebuild it whenever git-sync checks out a new commit.

git-sync keeps the checkout behind a symlink (REPO_LINK) and swaps it
atomically on every new commit, so watching where the link points is enough to
know when to rebuild. A failed build keeps serving the last good graph.

Pointed at a plain working copy instead (local development), it rebuilds when
HEAD moves or the working tree changes, so edits show up without a commit.
"""

from __future__ import annotations

import hashlib
import http.server
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from generator.build import write

REPO_LINK = Path(os.environ.get("REPO_LINK", "/repo/current"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
WEB_DIR = Path(os.environ.get("WEB_DIR", "/app/web"))
PORT = int(os.environ.get("PORT", "8080"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))

GRAPH = DATA_DIR / "graph.json"
log = logging.getLogger("docs")


def fingerprint(target: Path) -> str:
    """What the map was built from. git-sync's checkouts never change once
    made, so their path is enough; a working copy also needs HEAD and its
    uncommitted changes."""
    if REPO_LINK.is_symlink():
        return str(target)
    git = ["git", "-C", str(target)]
    out = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True).stdout
    out += subprocess.run(git + ["diff", "HEAD"], capture_output=True).stdout
    out += subprocess.run(git + ["ls-files", "--others", "--exclude-standard", "-s"], capture_output=True).stdout
    for f in subprocess.run(git + ["ls-files", "--others", "--exclude-standard"], capture_output=True, text=True).stdout.split():
        try:
            out += str((target / f).stat().st_mtime_ns).encode()
        except OSError:
            pass
    return f"{target}:{hashlib.sha256(out).hexdigest()}"


def watch() -> None:
    built_for = None
    while True:
        try:
            target = REPO_LINK.resolve(strict=True)
            key = fingerprint(target)
        except FileNotFoundError:
            target = key = None
        if target is not None and key != built_for:
            try:
                data = write(target, GRAPH)
                built_for = key
                log.info("built %s: %d nodes, %d edges, %d unresolved, errors: %s", data["sha"][:7],
                         len(data["nodes"]), len(data["edges"]), len(data["unresolved"]), data["errors"] or "none")
            except Exception:
                log.exception("build failed for %s; still serving the previous graph", target)
                built_for = key
        time.sleep(POLL_SECONDS)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._text(200 if GRAPH.exists() else 503, "ok" if GRAPH.exists() else "no graph yet")
        if path == "/graph.json":
            if not GRAPH.exists():
                return self._text(503, "the first build has not finished")
            body = GRAPH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        super().end_headers()

    def _text(self, code: int, text: str):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/healthz"):
            log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch, daemon=True).start()
    http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
