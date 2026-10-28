"""Terraform stacks: one node per resource instance, grouped by stack and
module, under the provider the stack is written for.

A stack is a directory with a `backend` block. Variables come from defaults and
terraform.tfvars, locals and for_each are evaluated (hcleval), and modules are
followed into their directories. Resources that only configure another one
(a bucket's versioning, a role's policy) are folded into it, so their comments
show up on the bucket or the role rather than as boxes of their own.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from ..hcl import Scan
from ..hcleval import UNKNOWN, WILD, Block, Evaluator, apply_type, display, parse
from ..model import Graph, Node
from ..source import Repo

# How a resource type is drawn. Anything not listed is "config", and is folded
# into the resource it configures when there is exactly one.
TYPE_KIND = {
    "aws_s3_bucket": "bucket",
    "b2_bucket": "bucket",
    "aws_ssm_parameter": "secret",
    "cloudflare_dns_record": "dns",
    "aws_iam_role": "cloud",
    "aws_iam_openid_connect_provider": "cloud",
    "aws_budgets_budget": "cloud",
    "cloudflare_zero_trust_tunnel_cloudflared": "cloud",
    "cloudflare_ruleset": "cloud",
    "cloudflare_certificate_authorities_hostname_associations": "cloud",
    "cloudflare_zone_setting": "cloud",
}

# The attribute that names a resource, by kind, for its label and its id.
NAME_ATTRS = ("name", "bucket", "bucket_name", "fqdn", "url", "budget_name")

PROVIDER_LABEL = {"aws": "AWS", "cloudflare": "Cloudflare", "b2": "Backblaze B2"}

# Attributes whose URL is somewhere traffic is sent: a cloudflared ingress
# rule's service. Other URLs (an OIDC issuer) are identifiers, not edges.
URL_ATTRS = ("service",)

_URL = re.compile(r"\b(https?|tcp|ssh)://([\w.-]+)(?::(\d+))?")


@dataclass
class Instance:
    id: str
    type: str
    kind: str
    key: object
    value: object = None
    refs: list[tuple[str, str]] = field(default_factory=list)  # (target address, index expr)


class Scope:
    """One module instance: a root stack or a module call inside one."""

    def __init__(self, repo: Repo, graph: Graph, dir_: str, prefix: str, parent_node: str, var: dict) -> None:
        self.repo = repo
        self.graph = graph
        self.dir = dir_
        self.prefix = prefix
        self.parent_node = parent_node
        self.ev = Evaluator()
        self.var = var
        self.local: dict = {}
        self.module: dict = {}
        self.instances: dict[str, list[Instance]] = {}
        self.deferred: list[tuple[str, str, str]] = []
        self.files = [f for f in repo.files(f"{dir_}/*.tf")]
        self.items: list[tuple[str, object, object]] = []  # (file, parsed item, span)
        for f in self.files:
            text = repo.text(f)
            scan = Scan(text)
            for item, span in zip(parse(text), scan.top()):
                self.items.append((f, item, span))

    @property
    def env(self) -> dict:
        return {"var": self.var, "local": self.local, "module": self.module}

    def blocks(self, type_: str):
        for f, item, span in self.items:
            if isinstance(item, Block) and item.type == type_:
                yield f, item, span

    def _variables(self) -> None:
        for _, b, _ in self.blocks("variable"):
            name = b.labels[0]
            if name not in self.var:
                self.var[name] = self.ev.eval(b.attrs["default"], {}) if "default" in b.attrs else None
            self.var[name] = apply_type(b.attrs.get("type"), self.var[name])

    def run(self) -> None:
        self._variables()
        # Locals may reference each other in any order; a few passes settle them.
        for _ in range(4):
            for _, b, _ in self.blocks("locals"):
                for k, v in b.attrs.items():
                    self.local[k] = self.ev.eval(v, self.env)
            for f, b, span in self.blocks("module"):
                self._module(f, b, span, final=False)
        for f, b, span in self.blocks("module"):
            self._module(f, b, span, final=True)
        for f, b, span in self.blocks("resource"):
            self._resource(f, b, span)
        self._link()

    # --- modules ---------------------------------------------------------
    def _module(self, file: str, b: Block, span, final: bool) -> None:
        name = b.labels[0]
        source = self.ev.eval(b.attrs.get("source"), self.env)
        if not isinstance(source, str) or not source.startswith("."):
            return
        mdir = posixpath.normpath(posixpath.join(self.dir, source))
        args = {k: self.ev.eval(v, self.env) for k, v in b.attrs.items() if k not in ("source", "version", "providers")}
        node_id = f"{self.prefix}:module.{name}"
        child = Scope(self.repo, self.graph, mdir, node_id, node_id, dict(args))
        if final:
            src = self.repo.source(file, span.start, span.end)
            self.graph.add(
                Node(
                    node_id,
                    "tfmodule",
                    f"module.{name}",
                    parent=self.parent_node,
                    attrs={"source": mdir, **_literal_attrs(args)},
                    sources=[src],
                )
            )
            child.run()
            self.module[name] = child.outputs()
            # Every name the module hands back (SSM paths, role names), so a
            # workflow naming one links to the module that grants it.
            self.graph.nodes[node_id].attrs["outputs"] = sorted(
                {s for s in _all_strings(self.module[name]) if WILD not in s and len(s) <= 200}
            )
            # Its variables after defaults, so a workflow's environment name
            # (plan_environment = "aws-plan") can be found.
            self.graph.nodes[node_id].attrs["variables"] = {
                k: v for k, v in _literal_attrs(child.var).items() if not isinstance(v, list)
            }
            for target, _ in _refs_in(b):
                self._defer_ref(node_id, target, "")
        else:
            child.run_values_only()
            self.module[name] = child.outputs()

    def run_values_only(self) -> None:
        self._variables()
        for _ in range(4):
            for _, b, _ in self.blocks("locals"):
                for k, v in b.attrs.items():
                    self.local[k] = self.ev.eval(v, self.env)
            for f, b, span in self.blocks("module"):
                self._module(f, b, span, final=False)

    def outputs(self) -> dict:
        out = {}
        for _, b, _ in self.blocks("output"):
            # Outputs may name resource attributes, which are unknown; the
            # names and paths built from inputs are what matter here.
            out[b.labels[0]] = self.ev.eval(b.attrs.get("value"), self.env)
        return out

    # --- resources -------------------------------------------------------
    def _resource(self, file: str, b: Block, span) -> None:
        rtype, rname = b.labels
        kind = TYPE_KIND.get(rtype, "config")
        each_items: list[tuple[object, object]] = [(None, None)]
        if "for_each" in b.attrs:
            fe = self.ev.eval(b.attrs["for_each"], self.env)
            if isinstance(fe, dict):
                each_items = list(fe.items())
            elif isinstance(fe, list):
                each_items = [(v, v) for v in fe]
            else:
                each_items = [("*", UNKNOWN)]
        elif "count" in b.attrs:
            n = self.ev.eval(b.attrs["count"], self.env)
            if n == 0:
                return
        src = self.repo.source(file, span.start, span.end)
        addr = f"{rtype}.{rname}"
        for key, value in each_items:
            env = dict(self.env)
            env["each"] = {"key": key, "value": value}
            attrs = {k: self.ev.eval(v, env) for k, v in b.attrs.items() if k not in ("for_each", "count", "depends_on", "lifecycle")}
            for nb in b.blocks:
                if nb.type in ("lifecycle",):
                    continue
                nested = {k: self.ev.eval(v, env) for k, v in nb.attrs.items()}
                attrs.setdefault(nb.type, nested)
            label = next((display(attrs[a]) for a in NAME_ATTRS if isinstance(attrs.get(a), str)), None)
            suffix = f'["{key}"]' if key is not None else ""
            if label is None:
                short = rtype.split("_", 1)[1] if "_" in rtype else rtype
                label = f"{rname} · {short}" + (f" [{key}]" if key is not None else "")
            node_id = self._node_id(rtype, attrs, f"{self.prefix}:{addr}{suffix}")
            sources = [src]
            tfvars = self._tfvars_entry(key, value)
            if tfvars:
                sources.append(tfvars)
            self.graph.add(
                Node(
                    node_id,
                    kind,
                    label,
                    parent=self.parent_node,
                    attrs={"resource": f"{addr}{suffix}", **_literal_attrs(attrs)},
                    sources=sources,
                )
            )
            inst = Instance(node_id, rtype, kind, key, value)
            for target, index in _refs_in(b):
                inst.refs.append((target, index))
            self.instances.setdefault(addr, []).append(inst)
            self._urls(node_id, attrs)

    def _node_id(self, rtype: str, attrs: dict, default: str) -> str:
        """Ids other sources can name too: SSM parameters, DNS records."""
        name = attrs.get("name")
        if rtype == "aws_ssm_parameter" and isinstance(name, str) and WILD not in name:
            return f"secret:ssm:{name}"
        if rtype == "cloudflare_dns_record" and isinstance(name, str) and WILD not in name:
            return f"dns:{name}:{attrs.get('type', '')}"
        return default

    def _tfvars_entry(self, key, value):
        """Where in terraform.tfvars this instance's for_each entry is written."""
        tfvars = f"{self.dir}/terraform.tfvars"
        if key is None or not self.repo.exists(tfvars):
            return None
        candidates = [key] + ([v for v in value.values() if isinstance(v, str)] if isinstance(value, dict) else [])
        scan = Scan(self.repo.text(tfvars))
        for top in scan.top():
            for entry in scan.inside(top):
                if entry.name in candidates:
                    return self.repo.source(tfvars, entry.start, entry.end)
        return None

    def _urls(self, node_id: str, attrs: dict) -> None:
        """A URL in a resource (a tunnel's ingress service) names a host and a
        port that nothing else in Terraform declares."""
        for s in _service_urls(attrs):
            for m in _URL.finditer(s):
                host, port = m.group(2), m.group(3)
                ref = f"refhost:{host}"
                self.graph.add(Node(ref, "refhost", host, attrs={"named in": "a URL"}))
                if port:
                    pid = f"port:{ref}:{port}"
                    self.graph.add(Node(pid, "port", port, parent=ref))
                    self.graph.link(node_id, pid, "forwards to")
                else:
                    self.graph.link(node_id, ref, "forwards to")

    # --- references ------------------------------------------------------
    def _defer_ref(self, src: str, target: str, index: str) -> None:
        self.deferred.append((src, target, index))

    def _link(self) -> None:
        by_id = {i.id: i for insts in self.instances.values() for i in insts}
        pending = [(i, t, x) for insts in self.instances.values() for i in insts for t, x in i.refs]
        for src, target, index in self.deferred:
            pending.append((by_id.get(src) or Instance(src, "module", "tfmodule", None), target, index))
        fold: dict[str, list[str]] = {}
        for inst, target, index in pending:
            if target.startswith("module."):
                tid = f"{self.prefix}:{target}"
                if tid in self.graph.nodes:
                    self.graph.link(inst.id, tid, "references")
                continue
            targets = self.instances.get(target, [])
            if index == "each.key" and inst.key is not None:
                targets = [t for t in targets if t.key == inst.key] or targets
            elif index.startswith("each.value.") and isinstance(inst.value, dict):
                # A value from this instance's own for_each entry, such as
                # each.value.tunnel: link only to that key.
                want = inst.value.get(index.split(".", 2)[2])
                targets = [t for t in targets if t.key == want] or targets
            for t in targets:
                if inst.kind == "config" and t.kind != "config":
                    fold.setdefault(inst.id, []).append(t.id)
                self.graph.link(inst.id, t.id, "references")
        for cid, owners in fold.items():
            owners = list(dict.fromkeys(owners))
            if len(owners) == 1:
                _fold(self.graph, cid, owners[0])


def _fold(graph: Graph, child_id: str, owner_id: str) -> None:
    child = graph.nodes.pop(child_id, None)
    owner = graph.nodes.get(owner_id)
    if child is None or owner is None:
        return
    owner.sources.extend(child.sources)
    owner.attrs.setdefault("configured by", []).append(child.attrs.get("resource", child.label))
    for e in graph.edges:
        if e.source == child_id:
            e.source = owner_id
        if e.target == child_id:
            e.target = owner_id
    graph.edges = [e for e in graph.edges if e.source != e.target]


_ADDR = re.compile(r"\b((?:module|data)\.[\w-]+(?:\.[\w-]+)?|[a-z][a-z0-9]*_[a-z0-9_]+\.[\w-]+)(\[[^\]]*\])?")


def _refs_in(b: Block) -> list[tuple[str, str]]:
    """Resource and module addresses an item's expressions mention, with the
    index expression that follows each one."""
    out = []
    for text in _expr_texts(b):
        for m in _ADDR.finditer(text):
            addr, idx = m.group(1), (m.group(2) or "")[1:-1].strip()
            head = addr.split(".")[0]
            if head in ("var", "local", "each", "count", "path", "terraform", "self", "data"):
                continue
            if head == "module":
                addr = ".".join(addr.split(".")[:2])
            out.append((addr, idx))
    return out


def _expr_texts(b: Block) -> list[str]:
    from lark import Token, Tree

    def flat(t) -> str:
        if isinstance(t, Token):
            return str(t)
        return "".join(flat(c) for c in t.children)

    texts = [flat(v) for v in b.attrs.values()]
    for nb in b.blocks:
        texts += _expr_texts(nb)
    return texts


def _service_urls(v, key=None):
    if isinstance(v, str):
        if key in URL_ATTRS:
            yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _service_urls(x, k)
    elif isinstance(v, list):
        for x in v:
            yield from _service_urls(x, key)


def _all_strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _all_strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _all_strings(x)


def _literal_attrs(attrs: dict) -> dict:
    """What is worth showing: known scalars and short lists, never policies."""
    out = {}
    for k, v in attrs.items():
        if k.startswith("_") or k in ("policy", "assume_role_policy", "value", "value_wo"):
            continue
        if isinstance(v, (str, int, float, bool)) and v is not None:
            s = display(v)
            if len(s) <= 200:
                out[k] = s if isinstance(v, str) else v
        elif isinstance(v, list) and v and all(isinstance(x, (str, int)) for x in v) and len(v) <= 12:
            out[k] = [display(x) for x in v]
    return out


def extract(repo: Repo, graph: Graph) -> None:
    stacks = []
    for f in repo.files("**/*.tf"):
        if f.startswith("modules/"):
            continue
        if re.search(r'^\s*backend\s+"', repo.text(f), re.M):
            stacks.append(posixpath.dirname(f))
    for stack in sorted(set(stacks)):
        _stack(repo, graph, stack)


def _stack(repo: Repo, graph: Graph, stack: str) -> None:
    stack_id = f"tf:{stack}"
    providers = []
    backend = {}
    ev = Evaluator()
    for f in repo.files(f"{stack}/*.tf"):
        for item in parse(repo.text(f)):
            if isinstance(item, Block) and item.type == "provider":
                providers.append(item.labels[0])
            if isinstance(item, Block) and item.type == "terraform":
                for nb in item.blocks:
                    if nb.type == "backend":
                        backend = {k: ev.eval(v, {}) for k, v in nb.attrs.items()}
    provider = providers[0] if providers else None
    parent = None
    if provider:
        parent = f"provider:{provider}"
        graph.add(Node(parent, "provider", PROVIDER_LABEL.get(provider, provider)))
    readme = next((f for f in (f"{stack}/README.md",) if repo.exists(f)), None)
    main = f"{stack}/main.tf" if repo.exists(f"{stack}/main.tf") else repo.files(f"{stack}/*.tf")[0]
    src = repo.source(main, 1, len(repo.lines(main)), inner=False, leading=False)
    src.comments = repo.header(main)
    graph.add(
        Node(
            stack_id,
            "tfstack",
            stack,
            parent=parent,
            attrs={
                "providers": providers,
                "state bucket": backend.get("bucket"),
                "state key": backend.get("key"),
                **({"readme": readme} if readme else {}),
            },
            sources=[src],
        )
    )
    scope = Scope(repo, graph, stack, stack_id, stack_id, {})
    tfvars = f"{stack}/terraform.tfvars"
    if repo.exists(tfvars):
        for item in parse(repo.text(tfvars)):
            if isinstance(item, tuple):
                scope.var[item[0]] = scope.ev.eval(item[1], {})
    scope.run()
