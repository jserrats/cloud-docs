"""Links between sources that only meet by name: a route and the public DNS
record with the same name, a workflow's SSM parameter and the module that
grants it, a stack's backend and its state bucket, a GitHub environment and the
module whose trust policy names it.

Also gives loose nodes a home in the layout, and reports what is named but
never declared.
"""

from __future__ import annotations

from .hcleval import WILD, matches
from .model import Graph, Node
from .source import Repo


def resolve(graph: Graph, repo: Repo) -> None:
    nodes = list(graph.nodes.values())
    by_kind: dict[str, list[Node]] = {}
    for n in nodes:
        by_kind.setdefault(n.kind, []).append(n)

    # Public DNS records and the routes Traefik serves under the same name.
    dns_by_name = {n.label: n for n in by_kind.get("dns", [])}
    for r in by_kind.get("route", []):
        for name in r.attrs.get("hosts", [r.label]):
            if name in dns_by_name:
                graph.link(dns_by_name[name].id, r.id, "same name as")

    # SSM parameters: granted by a module, created by a resource or by hand.
    modules = by_kind.get("tfmodule", [])
    for s in by_kind.get("secret", []):
        if not s.id.startswith("secret:ssm:"):
            continue
        name = s.label
        for m in modules:
            if name in m.attrs.get("outputs", []):
                graph.link(m.id, s.id, "grants read on")
                s.parent = s.parent or m.parent
        if not any(src.file.endswith(".tf") for src in s.sources):
            s.attrs["created"] = "by hand: no resource declares it"
            first = s.sources[0] if s.sources else None
            graph.unresolved_ref(name, first.file if first else "", first.start if first else 0,
                                 "an SSM parameter read in CI that no Terraform resource creates")

    # A stack's backend names its state bucket literally; the bucket's own
    # name is built at apply time, so match it as a pattern.
    for st in by_kind.get("tfstack", []):
        want = st.attrs.get("state bucket")
        for b in by_kind.get("bucket", []):
            if want and matches(b.label.replace("…", WILD), want):
                graph.link(st.id, b.id, "keeps state in")

    # GitHub environments and the module whose OIDC trust subjects name them
    # (repo:<owner>/<repo>:environment:<name>).
    for env in by_kind.get("environment", []):
        for m in modules:
            if any(o.endswith(f":environment:{env.label}") for o in m.attrs.get("outputs", [])):
                graph.link(env.id, m.id, "trusted by")

    # Hosts named only in strings.
    for ref in by_kind.get("refhost", []):
        ref.parent = ref.parent or "group:refhosts"
        file, line = _first_mention(graph, repo, ref)
        graph.unresolved_ref(ref.label, file, line,
                             "a host named in a URL or ExternalName that the inventory does not declare")
    if by_kind.get("refhost"):
        graph.add(Node("group:refhosts", "provider", "Referenced hosts",
                       attrs={"what": "Hosts the code sends traffic to without declaring them"}))

    # SSM parameters with no stack of their own sit under AWS.
    if "provider:aws" in graph.nodes:
        for s in by_kind.get("secret", []):
            if s.id.startswith("secret:ssm:") and not s.parent:
                s.parent = "provider:aws"

    # A DNS record's reference is where it points.
    for e in graph.edges:
        if e.source in graph.nodes and graph.nodes[e.source].kind == "dns" and e.kind == "references":
            e.kind = "points to"


def _first_mention(graph: Graph, repo: Repo, ref: Node) -> tuple[str, int]:
    """Where a referenced host is first written, among the files of the nodes
    that point at it."""
    for e in graph.edges:
        if e.target != ref.id and not e.target.startswith(f"port:{ref.id}:"):
            continue
        src = graph.nodes.get(e.source)
        for s in src.sources if src else []:
            for i, line in enumerate(repo.lines(s.file), 1):
                if ref.label in line and not line.lstrip().startswith("#"):
                    return s.file, i
    return "", 0
