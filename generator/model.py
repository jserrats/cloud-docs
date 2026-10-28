"""The graph the generator emits: nodes, edges and unresolved references.

A node can be declared in more than one place (a VM in host_vars and the same
machine in the inventory), so it carries a list of sources, each with its own
file, line range and comments.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every kind the UI knows how to draw. Extractors must use one of these.
KINDS = {
    "provider",   # a cloud provider or GitHub: a grouping box
    "tfstack",    # a Terraform root module (aws/bootstrap, ...)
    "tfmodule",   # a module call inside a stack
    "host",       # a machine in the Ansible inventory
    "vm",         # a Proxmox VM (also an inventory host)
    "refhost",    # a host only named in a string (an ExternalName, a URL)
    "cluster",    # a k3s cluster
    "helm",       # an ArgoCD Application or a HelmChart
    "stack",      # a compose project
    "container",  # a compose service
    "route",      # a hostname a Traefik serves
    "port",       # a port a container or service listens on
    "dns",        # a public DNS record
    "network",    # a docker network or a subnet
    "disk",       # a VM disk, a volume, a shared directory
    "bucket",     # an S3 or B2 bucket
    "cloud",      # any other cloud resource (IAM, tunnel, WAF, budget)
    "config",     # a cloud resource that only configures another one
    "secret",     # a secret's name and where its value lives, never the value
    "workflow",   # a GitHub Actions workflow
    "job",        # a job in a workflow
    "environment",  # a GitHub deployment environment
}


@dataclass
class Source:
    file: str
    start: int
    end: int
    comments: list[str] = field(default_factory=list)


@dataclass
class Node:
    id: str
    kind: str
    label: str
    parent: str | None = None
    attrs: dict = field(default_factory=dict)
    sources: list[Source] = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    kind: str


@dataclass
class Unresolved:
    text: str
    file: str
    line: int
    why: str


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.unresolved: list[Unresolved] = []

    def add(self, node: Node) -> Node:
        """Add a node, or merge it into the one already under its id.

        Merging keeps the first kind and label unless the newcomer is more
        specific (a vm over a host), adds sources, and fills in attributes and
        the parent without overwriting them.
        """
        if node.kind not in KINDS:
            raise ValueError(f"unknown kind {node.kind!r} for {node.id}")
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        if existing.kind in ("host", "refhost") and node.kind == "vm":
            existing.kind = "vm"
        if existing.parent is None:
            existing.parent = node.parent
        for k, v in node.attrs.items():
            existing.attrs.setdefault(k, v)
        for s in node.sources:
            if not any(s.file == o.file and s.start == o.start for o in existing.sources):
                existing.sources.append(s)
        return existing

    def merge(self, old_id: str, into_id: str) -> None:
        """Fold one node into another that turned out to be the same thing:
        its sources, attributes, children and edges move over, and it goes."""
        old = self.nodes.pop(old_id, None)
        into = self.nodes.get(into_id)
        if old is None or into is None or old is into:
            return
        old.id = into_id
        old.kind = into.kind
        old.parent = None
        self.add(old)
        for n in self.nodes.values():
            if n.parent == old_id:
                n.parent = into_id
        for e in self.edges:
            if e.source == old_id:
                e.source = into_id
            if e.target == old_id:
                e.target = into_id
        seen = set()
        edges = []
        for e in self.edges:
            key = (e.source, e.target, e.kind)
            if e.source != e.target and key not in seen:
                seen.add(key)
                edges.append(e)
        self.edges = edges

    def link(self, source: str, target: str, kind: str) -> None:
        if source == target:
            return
        if any(e.source == source and e.target == target and e.kind == kind for e in self.edges):
            return
        self.edges.append(Edge(source, target, kind))

    def unresolved_ref(self, text: str, file: str, line: int, why: str) -> None:
        self.unresolved.append(Unresolved(text, file, line, why))

    def finish(self) -> None:
        """Drop edges to nodes nobody declared, and parents that do not exist."""
        for n in self.nodes.values():
            if n.parent and n.parent not in self.nodes:
                n.parent = None
        self.edges = [e for e in self.edges if e.source in self.nodes and e.target in self.nodes]

    def to_json(self) -> dict:
        return {
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "label": n.label,
                    "parent": n.parent,
                    "attrs": n.attrs,
                    "sources": [vars(s) for s in n.sources],
                }
                for n in self.nodes.values()
            ],
            "edges": [vars(e) for e in self.edges],
            "unresolved": [vars(u) for u in self.unresolved],
        }
