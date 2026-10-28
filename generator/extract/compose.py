"""Compose stacks under docker/<host>/<stack>/: containers, published ports,
volumes, networks, Traefik routes from labels and from the file provider, and
the variables whose values live outside the repo.

`${VAR}` is resolved from the .env beside the compose file, the way compose
itself does. A variable with no value there is left as written and reported as
unresolved; one named like a credential is drawn as a secret.
"""

from __future__ import annotations

import posixpath
import re

from ..documented import lookup
from ..model import Graph, Node
from ..source import Repo
from ..yamlsrc import entries, entry, load, plain

ROOT = "docker"
COMPOSE_NAMES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")

# `$${X}` is compose's escape for a literal `${X}`, left to the container.
_VAR = re.compile(r"(?<!\$)\$\{(\w+)(?::?([?-])([^}]*))?\}")
_HOST_RULE = re.compile(r"Host\(`([^`]+)`\)")
_SECRET_NAME = re.compile(r"(TOKEN|PASSWORD|PASS|SECRET|_KEY)$", re.I)
_URL = re.compile(r"\b(https?)://([\w.-]+)(?::(\d+))?")


def extract(repo: Repo, graph: Graph) -> None:
    for f in sorted(f for n in COMPOSE_NAMES for f in repo.files(f"{ROOT}/*/*/{n}")):
        _stack(repo, graph, f)


def _env(repo: Repo, d: str) -> tuple[str | None, dict[str, tuple[str, int]]]:
    f = f"{d}/.env"
    if not repo.exists(f):
        return None, {}
    out = {}
    for i, line in enumerate(repo.lines(f), 1):
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*=\s*(.*)$", line)
        if m:
            out[m.group(1)] = (m.group(2).strip().strip("\"'"), i)
    return f, out


def _stack(repo: Repo, graph: Graph, f: str) -> None:
    d = posixpath.dirname(f)
    _, host, stack = d.split("/")[:3]
    sid = f"stack:{host}/{stack}"
    env_file, env = _env(repo, d)
    deployed_by = _deployed_by(repo, f)
    src = repo.source(f, 1, len(repo.lines(f)), inner=False, leading=False)
    src.comments = repo.header(f)
    host_id = f"host:{host}"
    if host_id not in graph.nodes:
        graph.unresolved_ref(host, f, 1, "a docker/<host>/ directory with no such host in the Ansible inventory")
    graph.add(
        Node(sid, "stack", stack, parent=host_id,
             attrs={"compose file": f, **({"env file": env_file} if env_file else {}),
                    "deployed by": deployed_by or "not stated in code",
                    "host": f"{host} (from the directory name)"},
             sources=[src])
    )
    seen_unresolved: set[str] = set()

    def resolve(s: str, line: int) -> str:
        def sub(m):
            name, op, arg = m.group(1), m.group(2), m.group(3)
            value = env.get(name, ("", 0))[0]
            if value:
                return value
            # Set outside the repo, as docs/homelab.yaml records it.
            doc = lookup(repo, d, name)
            if doc is not None:
                stack_node = graph.nodes[sid]
                stack_node.attrs.setdefault("documented variables", {})[name] = doc.value
                if not any(s.file == doc.source.file and s.start == doc.source.start for s in stack_node.sources):
                    stack_node.sources.append(doc.source)
                return doc.value
            if op == "-" and arg:
                return arg
            if name not in seen_unresolved:
                seen_unresolved.add(name)
                _unset_var(repo, graph, sid, f, line, name, env_file, env)
            return "${" + name + "}"

        return _VAR.sub(sub, s)

    doc = load(repo, f)[0]
    top_volumes = plain(doc.value.get("volumes") or {})
    services = entry(repo, f, doc.value, "services", doc.end)
    for svc in entries(repo, f, services.value, services.end) if services else []:
        _service(repo, graph, f, host, stack, sid, svc, top_volumes, resolve)

    # Traefik's file provider: routes for hosts that are not containers.
    for dyn in repo.files(f"{d}/dynamic/*.yml") + repo.files(f"{d}/dynamic/*.yaml"):
        _dynamic(repo, graph, dyn, host, sid, env)


def _deployed_by(repo: Repo, f: str) -> str | None:
    """An Ansible role that copies this compose file is what deploys it."""
    for t in repo.files("ansible/roles/*/tasks/*.yml"):
        if f in repo.text(t):
            return f"ansible role {t.split('/')[2]}"
    return None


def _unset_var(repo, graph, sid, f, line, name, env_file, env) -> None:
    where = f"{env_file}, empty" if name in env else "no .env entry" if env_file else "no .env file"
    if _SECRET_NAME.search(name):
        sec = f"secret:env:{sid[6:]}/{name}"
        sources = [repo.source(env_file, env[name][1], env[name][1])] if name in env else [repo.source(f, line, line)]
        graph.add(Node(sec, "secret", name, parent=sid,
                       attrs={"store": "outside the repo", "declared": where}, sources=sources))
    else:
        graph.unresolved_ref("${" + name + "}", f, line, f"a compose variable with no value in the repo ({where})")


def _service(repo, graph, f, host, stack, sid, svc, top_volumes, resolve) -> None:
    s = svc.value
    cid = f"ctr:{host}/{stack}/{svc.key}"
    sp = plain(s)
    attrs = {"image": sp.get("image", "(built: " + str(sp.get("build")) + ")")}
    for k in ("container_name", "restart", "network_mode", "user"):
        if k in sp:
            attrs[k] = sp[k]
    graph.add(Node(cid, "container", str(svc.key), parent=sid, attrs=attrs, sources=[repo.source(f, svc.start, svc.end)]))

    def line_of(key):
        e = entry(repo, f, s, key, svc.end)
        return e.start if e else svc.start

    # Variables the service reads: secrets it uses, unresolved ones reported.
    for m in _VAR.finditer("\n".join(repo.lines(f)[svc.start - 1 : svc.end])):
        resolve(m.group(0), svc.start)
        sec = f"secret:env:{host}/{stack}/{m.group(1)}"
        if sec in graph.nodes:
            graph.link(cid, sec, "reads")

    nm = sp.get("network_mode", "")
    if isinstance(nm, str) and nm.startswith("service:"):
        graph.link(cid, f"ctr:{host}/{stack}/{nm[8:]}", "shares the network of")

    for p in sp.get("ports", []) or []:
        spec = resolve(str(p if not isinstance(p, dict) else f"{p.get('published')}:{p.get('target')}"), line_of("ports"))
        published = spec.rsplit(":", 1)[0] if ":" in spec else spec
        pid = f"port:{cid}:{published}"
        graph.add(Node(pid, "port", spec, parent=cid, attrs={"published on": host},
                       sources=[repo.source(f, line_of("ports"), line_of("ports"))]))

    nets = sp.get("networks") or []
    for n in (nets if isinstance(nets, list) else list(nets)):
        if f"net:{host}:{n}" in graph.nodes:
            graph.link(cid, f"net:{host}:{n}", "joins")

    for v in sp.get("volumes", []) or []:
        spec = resolve(v if isinstance(v, str) else f"{v.get('source')}:{v.get('target')}", line_of("volumes"))
        src_path = spec.split(":", 1)[0]
        if src_path in top_volumes:
            vid = f"vol:{host}/{stack}/{src_path}"
            graph.add(Node(vid, "disk", src_path, parent=sid, attrs={"type": "docker named volume"},
                           sources=[repo.source(f, line_of("volumes"), line_of("volumes"), inner=False)]))
            graph.link(cid, vid, "mounts")
            # Named volumes live on the host's data disk when it is mounted
            # where docker keeps them.
            for disk in graph.nodes.values():
                if disk.parent == f"host:{host}" and disk.attrs.get("mountpoint") == "/var/lib/docker/volumes":
                    graph.link(vid, disk.id, "stored on")
        elif src_path.startswith("/"):
            attrs.setdefault("bind mounts", []).append(spec)
            _link_host_path(graph, cid, host, src_path)

    labels = sp.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(l.split("=", 1) for l in labels if "=" in l)
    ports = {k.split(".")[3]: v for k, v in labels.items() if k.startswith("traefik.http.services.") and k.endswith(".loadbalancer.server.port")}
    for k, v in labels.items():
        if not (k.startswith("traefik.http.routers.") and k.endswith(".rule")):
            continue
        router = k.split(".")[3]
        line = next((i for i in range(svc.start, svc.end + 1) if k in repo.lines(f)[i - 1]), svc.start)
        # The labels block, with the comment above it on how routing works.
        le = entry(repo, f, s, "labels", svc.end)
        src = repo.source(f, le.start, le.end) if le else repo.source(f, line, line)
        for fqdn in _HOST_RULE.findall(resolve(str(v), line)):
            rid = f"route:{host}:{fqdn}"
            graph.add(Node(rid, "route", fqdn, parent=f"host:{host}",
                           attrs={"router": router, "served by": f"traefik on {host}"}, sources=[src]))
            service = labels.get(f"traefik.http.routers.{router}.service")
            port = ports.get(router) or (next(iter(ports.values())) if len(ports) == 1 else None)
            if port:
                pid = f"port:{cid}:{port}"
                graph.add(Node(pid, "port", str(port), parent=cid))
                graph.link(rid, pid, "routes to")
            elif service and "@internal" in service:
                graph.link(rid, cid, f"routes to ({service})")
            else:
                graph.link(rid, cid, "routes to")
            _served_by(graph, host, rid)


def _served_by(graph: Graph, host: str, rid: str) -> None:
    """The Traefik container on the same host serves every route there."""
    for n in list(graph.nodes.values()):
        if n.kind == "container" and n.id.startswith(f"ctr:{host}/") and str(n.attrs.get("image", "")).startswith("traefik"):
            graph.link(n.id, rid, "serves")


def _link_host_path(graph: Graph, cid: str, host: str, path: str) -> None:
    """A bind mount from a directory shared into the VM over virtiofs."""
    for e in list(graph.edges):
        if e.source == f"host:{host}" and e.kind == "mounts":
            share = graph.nodes.get(e.target)
            base = share.attrs.get("path") if share else None
            if base and (path == base or path.startswith(base.rstrip("/") + "/")):
                graph.link(cid, share.id, f"mounts {path}")


def _dynamic(repo: Repo, graph: Graph, f: str, host: str, sid: str, env: dict) -> None:
    doc = load(repo, f)
    if not doc:
        return
    http = entry(repo, f, doc[0].value, "http", doc[0].end)
    if http is None:
        return
    routers = entry(repo, f, http.value, "routers", http.end)
    services = plain(http.value.get("services") or {})
    for r in entries(repo, f, routers.value, routers.end) if routers else []:
        rule = str(r.value.get("rule", ""))
        rule = re.sub(r'\{\{\s*env\s+"(\w+)"\s*\}\}', lambda m: env.get(m.group(1), ("${" + m.group(1) + "}",))[0], rule)
        for fqdn in _HOST_RULE.findall(rule):
            rid = f"route:{host}:{fqdn}"
            src = repo.source(f, r.start, r.end)
            src.comments = repo.header(f) + src.comments
            graph.add(Node(rid, "route", fqdn, parent=f"host:{host}",
                           attrs={"router": str(r.key), "served by": f"traefik on {host} (file provider)"}, sources=[src]))
            _served_by(graph, host, rid)
            svc = services.get(str(r.value.get("service")), {})
            for server in (svc.get("loadBalancer") or {}).get("servers", []):
                m = _URL.search(server.get("url", ""))
                if m:
                    ref = f"refhost:{m.group(2)}"
                    graph.add(Node(ref, "refhost", m.group(2), attrs={"named in": "a URL"}))
                    target = ref
                    if m.group(3):
                        target = f"port:{ref}:{m.group(3)}"
                        graph.add(Node(target, "port", m.group(3), parent=ref))
                    graph.link(rid, target, "routes to")
