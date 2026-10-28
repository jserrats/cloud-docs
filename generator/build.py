"""build.py <checkout> <output.json>: parse a checkout of the cloud repo into
the graph the UI draws.

The commit and the GitHub URL come from the checkout itself, so every link
points at exactly what was parsed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .extract import ansible, argocd, compose, homelab, terraform, workflows
from .model import Graph
from .resolve import resolve
from .source import Repo

# Order matters: hosts and disks first, so compose and ArgoCD can attach to
# them; homelab.yaml last, because it joins what the others found.
EXTRACTORS = [ansible, terraform, workflows, compose, argocd, homelab]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def web_url(remote: str) -> str:
    """git@github.com:o/r.git or https://github.com/o/r.git -> https://github.com/o/r"""
    m = re.match(r"^(?:git@|ssh://git@)([^:/]+)[:/](.+?)(?:\.git)?$", remote)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git$", "", remote)


def build(root: Path) -> dict:
    sha = git(root, "rev-parse", "HEAD")
    # git-sync fetches by URL without naming a remote, so the URL can also come
    # from the environment: the same value the chart hands git-sync.
    remote = os.environ.get("REPO_URL") or git(root, "config", "--get", "remote.origin.url")
    repo = Repo(root, sha)
    graph = Graph()
    errors = []
    for ex in EXTRACTORS:
        try:
            ex.extract(repo, graph)
        except Exception as e:  # one broken extractor must not blank the map
            errors.append(f"{ex.__name__.rsplit('.', 1)[-1]}: {type(e).__name__}: {e}")
    resolve(graph, repo)
    graph.finish()
    out = graph.to_json()
    out.update(
        sha=sha,
        repo=web_url(remote),
        subject=git(root, "log", "-1", "--format=%s"),
        committed_at=git(root, "log", "-1", "--format=%cI"),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        errors=errors,
    )
    return out


def write(root: Path, dest: Path) -> dict:
    data = build(root)
    # Atomic: the server never serves a half-written file.
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".graph-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    return data


def main() -> None:
    root, dest = Path(sys.argv[1]), Path(sys.argv[2])
    data = write(root, dest)
    print(f"{data['sha'][:7]}: {len(data['nodes'])} nodes, {len(data['edges'])} edges, "
          f"{len(data['unresolved'])} unresolved, {len(data['errors'])} errors")
    for e in data["errors"]:
        print("error:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
