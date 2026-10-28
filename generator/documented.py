"""Values the repository documents for variables that are set outside it,
from the `variables` section of docs/homelab.yaml:

    variables:
      docker/europa/entertainment:
        DOMAIN: jserrats.xyz

Each key is a directory; a variable applies to every file under it, and the
longest matching directory wins. Read by the extractors that resolve
`${VAR}`, as the last place to look before calling a variable unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass

from .source import Repo, Source
from .yamlsrc import entries, entry, load

FILE = "docs/homelab.yaml"


@dataclass
class Documented:
    value: str
    scope: str
    source: Source


def _load(repo: Repo) -> dict[str, dict[str, Documented]]:
    cached = getattr(repo, "_documented", None)
    if cached is not None:
        return cached
    out: dict[str, dict[str, Documented]] = {}
    docs = load(repo, FILE) if repo.exists(FILE) else []
    if docs and isinstance(docs[0].value, dict):
        section = entry(repo, FILE, docs[0].value, "variables", docs[0].end)
        for scope in entries(repo, FILE, section.value, section.end) if section else []:
            path = str(scope.key).strip("/")
            for var in entries(repo, FILE, scope.value, scope.end):
                if var.value is None:
                    continue
                v = var.value
                text = ("true" if v else "false") if isinstance(v, bool) else str(v)
                # The scope's line too, so its comment comes along.
                src = repo.source(FILE, scope.start, var.end, inner=True)
                out.setdefault(path, {})[str(var.key)] = Documented(text, path, src)
    repo._documented = out
    return out


def lookup(repo: Repo, path: str, name: str) -> Documented | None:
    """The documented value of `name` for a file or directory at `path`."""
    best = None
    for scope, values in _load(repo).items():
        if name in values and (path == scope or path.startswith(scope + "/")):
            if best is None or len(scope) > len(best.scope):
                best = values[name]
    return best
