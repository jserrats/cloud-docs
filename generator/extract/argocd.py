"""ArgoCD: the Applications under argocd/ as Helm charts on the k3s cluster,
the routes the local ingress chart declares, the volumes and secrets the apps
ask for, and what each route forwards to.

What happens inside a chart (Deployments, Services, pods) is out of scope. From
a local chart only the things that connect it to the rest of the map are read:
InfisicalSecrets, PersistentVolumeClaims, hostPath volumes, ExternalName
Services and Traefik routes.
"""

from __future__ import annotations

import re

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..model import Graph, Node
from ..source import Repo
from ..yamlsrc import TPL, load, plain, span

ROOT = "argocd"
_HOST = re.compile(r"Host\(`([^`]+)`\)")
_SVC_URL = re.compile(r"://([\w-]+)\.([\w-]+)\.svc(?:\.cluster\.local)?(?::(\d+))?")


def extract(repo: Repo, graph: Graph) -> None:
    if not repo.exists(f"{ROOT}/root.yaml"):
        return
    cluster = next((n.id for n in graph.nodes.values() if n.kind == "cluster"), None)
    if cluster is None:
        cluster = "cluster:argocd"
        graph.add(Node(cluster, "cluster", "kubernetes"))
    values = {}
    if repo.exists(f"{ROOT}/cluster.yaml"):
        doc = load(repo, f"{ROOT}/cluster.yaml")[0]
        values = plain(doc.value)
        c = graph.nodes[cluster]
        c.attrs.update({k: v for k, v in values.items() if isinstance(v, str)})
        src = repo.source(f"{ROOT}/cluster.yaml", doc.start, doc.end)
        src.comments = repo.header(f"{ROOT}/cluster.yaml") + src.comments
        c.sources.append(src)

    # The root Application points ArgoCD at this repository; any other source
    # with the same URL is a path in this checkout.
    root_doc = plain(load(repo, f"{ROOT}/root.yaml")[0].value)
    this_repo = root_doc.get("spec", {}).get("source", {}).get("repoURL")
    apps: dict[str, dict] = {}
    root = _application(repo, graph, f"{ROOT}/root.yaml", cluster, apps, this_repo)
    for f in sorted(set(repo.files(f"{ROOT}/apps/**/*.yaml") + repo.files(f"{ROOT}/apps/*.yaml"))):
        _application(repo, graph, f, cluster, apps, this_repo, synced_by=root)

    helpers = _helpers(repo)
    for name, app in apps.items():
        for path in app["local_paths"]:
            _local_chart(repo, graph, path, name, app, apps, values, helpers)
    for name, app in apps.items():
        _values_links(repo, graph, name, app, apps)


def _application(repo: Repo, graph: Graph, f: str, cluster: str, apps: dict, this_repo: str | None,
                 synced_by: str | None = None):
    last = None
    for doc in load(repo, f):
        v = plain(doc.value)
        if not isinstance(v, dict) or v.get("kind") != "Application":
            continue
        name = v["metadata"]["name"]
        spec = v.get("spec", {})
        sources = spec.get("sources") or [spec.get("source", {})]
        attrs = {"namespace": spec.get("destination", {}).get("namespace")}
        local_paths, chart_sources = [], []
        for s in sources:
            if s.get("ref"):
                continue
            if s.get("chart"):
                chart_sources.append(f"{s['chart']} {s.get('targetRevision')} from {s.get('repoURL')}")
            elif s.get("path"):
                if s.get("repoURL") == this_repo:
                    local_paths.append(s["path"])
                    chart_sources.append(f"{s['path']} (this repo, {s.get('targetRevision')})")
                else:
                    chart_sources.append(f"{s['path']} of {s.get('repoURL')} at {s.get('targetRevision')}")
        attrs["source"] = chart_sources
        helm = [s.get("helm", {}) for s in sources if s.get("helm")]
        image_tags = [h.get("valuesObject", {}).get("image", {}).get("tag") for h in helm]
        if any(image_tags):
            attrs["image tag"] = next(t for t in image_tags if t)
        sync = spec.get("syncPolicy", {})
        if sync.get("syncOptions"):
            attrs["sync options"] = sync["syncOptions"]
        nid = f"argo:{name}"
        graph.add(Node(nid, "helm", name, parent=cluster, attrs=attrs, sources=[repo.source(f, doc.start, doc.end)]))
        if synced_by:
            graph.link(synced_by, nid, "syncs")
        apps[name] = {"id": nid, "file": f, "doc": doc, "spec": spec, "local_paths": local_paths,
                      "namespace": attrs["namespace"], "app_of_apps": synced_by is None}
        last = nid
    return last


def _helpers(repo: Repo) -> dict[str, str]:
    """Named templates from every local chart's _helpers.tpl, as their bodies."""
    out = {}
    for f in repo.files(f"{ROOT}/*/templates/_helpers.tpl"):
        for m in re.finditer(r'\{\{-?\s*define\s+"([^"]+)"\s*-?\}\}(.*?)\{\{-?\s*end\s*-?\}\}', repo.text(f), re.S):
            out[m.group(1)] = m.group(2)
    return out


def _render_helper(body: str, arg: str, values: dict) -> str:
    """Enough of a helper to read the hosts it builds: `$name` is the argument
    and `$values.X` (or .Values.X) is cluster.yaml."""
    body = re.sub(r"\{\{-?\s*\$\w+\s*:=.*?-?\}\}", "", body)
    body = re.sub(r"\{\{-?\s*\$name\s*-?\}\}", arg, body)
    body = re.sub(r"\{\{-?[^}]*?\$values\.(\w+)\s*-?\}\}", lambda m: str(values.get(m.group(1), "")), body)
    body = re.sub(r"\{\{-?[^}]*?\.Values\.(\w+)[^}]*?-?\}\}", lambda m: str(values.get(m.group(1), "")), body)
    return body.strip()


def _local_chart(repo, graph, path, app_name, app, apps, values, helpers) -> None:
    nid = app["id"]
    for f in repo.files(f"{path}/templates/*.yaml") + repo.files(f"{path}/templates/*.yml") + repo.files(f"{path}/*.yaml"):
        if f.endswith("Chart.yaml") or f.endswith("values.yaml"):
            continue
        docs = load(repo, f, template=True)
        externals = {}
        for d in docs:
            v = plain(d.value)
            if isinstance(v, dict) and v.get("kind") == "Service" and v.get("spec", {}).get("type") == "ExternalName":
                externals[(v["metadata"].get("namespace"), v["metadata"]["name"])] = v["spec"]["externalName"]
        for d in docs:
            v = plain(d.value)
            if not isinstance(v, dict):
                continue
            kind = v.get("kind")
            src = repo.source(f, d.start, d.end)
            if d is docs[0] or not src.comments:
                src.comments = _dedupe(repo.header(f) + src.comments)
            md = v.get("metadata", {})
            if kind == "InfisicalSecret":
                _infisical(graph, nid, v, src, values, apps)
            elif kind == "PersistentVolumeClaim":
                spec = v.get("spec", {})
                size = spec.get("resources", {}).get("requests", {}).get("storage")
                did = f"pvc:{app_name}:{md.get('name')}"
                graph.add(Node(did, "disk", f"{md.get('name')} {size}", parent=nid,
                               attrs={"type": "PersistentVolumeClaim", "storage class": spec.get("storageClassName"), "size": size},
                               sources=[src]))
                _storage_class(graph, did, spec.get("storageClassName"), apps)
            elif kind in ("Deployment", "StatefulSet", "DaemonSet"):
                for vol in v.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", []):
                    hp = vol.get("hostPath", {}).get("path")
                    if hp:
                        _host_path(graph, nid, hp)
            elif kind == "IngressRoute":
                _ingress_route(repo, graph, f, d, v, src, values, helpers, externals, apps, nid)
            elif kind == "Ingress":
                for rule in v.get("spec", {}).get("rules", []):
                    if rule.get("host"):
                        rid = f"route:{rule['host']}"
                        graph.add(Node(rid, "route", rule["host"], parent=graph.nodes[nid].parent, sources=[src]))
                        graph.link(nid, rid, "declares")
                        _served_by_traefik(graph, rid, apps)


def _dedupe(xs):
    return list(dict.fromkeys(xs))


def _infisical(graph, nid, v, src, values, apps) -> None:
    spec = v.get("spec", {})
    auth = spec.get("authentication", {}).get("kubernetesAuth", {})
    scope = auth.get("secretsScope", {})
    inf = values.get("infisical", {}) if isinstance(values.get("infisical"), dict) else {}
    project = _value(scope.get("projectSlug"), inf, "projectSlug")
    env = _value(scope.get("envSlug"), inf, "envSlug")
    path = scope.get("secretsPath")
    sid = f"secret:infisical:{project}/{env}{path}"
    targets = [f"{t.get('secretNamespace')}/{t.get('secretName')}" for t in spec.get("managedKubeSecretReferences", [])]
    graph.add(Node(sid, "secret", f"infisical {env}:{path}", parent=nid,
                   attrs={"store": "Infisical", "project": project, "environment": env, "folder": path,
                          "synced into": targets},
                   sources=[src]))
    graph.link(nid, sid, "reads")
    sa_ns = auth.get("serviceAccountRef", {}).get("namespace")
    for name, app in apps.items():
        if app["namespace"] == sa_ns:
            graph.link(sid, app["id"], "synced by")


def _value(raw, values: dict, key: str):
    """A template placeholder for .Values.X or a `with` block's .X, filled in
    from cluster.yaml."""
    if isinstance(raw, str) and raw.startswith(TPL):
        return values.get(key, raw)
    return raw


def _storage_class(graph, disk_id, cls, apps) -> None:
    """A StorageClass is provided by the app of the same name (longhorn)."""
    if cls and cls in apps:
        graph.link(disk_id, apps[cls]["id"], "stored on")


def _host_path(graph, nid, path) -> None:
    for n in list(graph.nodes.values()):
        base = n.attrs.get("path")
        if n.kind == "disk" and base and (path == base or path.startswith(base.rstrip("/") + "/")):
            graph.link(nid, n.id, f"mounts {path}")


def _served_by_traefik(graph, rid, apps) -> None:
    for name, app in apps.items():
        if any("traefik " in s and "traefik-" not in s for s in graph.nodes[app["id"]].attrs.get("source", [])):
            graph.link(app["id"], rid, "serves")


def _ingress_route(repo, graph, f, d, v, src, values, helpers, externals, apps, declared_by) -> None:
    ns = v.get("metadata", {}).get("namespace")
    raw_lines = repo.lines(f)[d.start - 1 : d.end]
    for route in v.get("spec", {}).get("routes", []):
        match = str(route.get("match", ""))
        hosts = []
        m = re.match(rf"{TPL}:([\w.-]+):(.+)$", match)
        if m and m.group(1) in helpers:
            hosts = _HOST.findall(_render_helper(helpers[m.group(1)], m.group(2), values))
        else:
            # A literal rule, or one with .Values in it.
            raw = next((l for l in raw_lines if "match:" in l), "")
            rendered = re.sub(r"\{\{[^}]*\.Values\.(\w+)[^}]*\}\}", lambda x: str(values.get(x.group(1), "")), raw)
            hosts = _HOST.findall(rendered)
            if not hosts and "HostRegexp" in rendered:
                hosts = ["*." + str(values.get("rootDomain", "")) + " (fallback)"]
        mws = [mw.get("name") for mw in route.get("middlewares", [])]
        if not hosts:
            continue
        # One node per route, under every name it matches: the shortest name
        # is the label, the rest are attributes.
        rid = f"route:{sorted(hosts, key=len)[0]}"
        attrs = {"hosts": hosts, "served by": "traefik on the cluster", "namespace": ns}
        if mws:
            attrs["middlewares"] = mws
        if route.get("priority"):
            attrs["priority"] = route["priority"]
        graph.add(Node(rid, "route", sorted(hosts, key=len)[0], parent=graph.nodes[declared_by].parent,
                       attrs=attrs, sources=[src]))
        graph.link(declared_by, rid, "declares")
        _served_by_traefik(graph, rid, apps)
        for svc in route.get("services", []):
            _route_target(graph, rid, ns, svc, externals, apps)


def _route_target(graph, rid, ns, svc, externals, apps) -> None:
    name, port = svc.get("name"), svc.get("port")
    if svc.get("kind") == "TraefikService":
        return
    ext = externals.get((ns, name))
    if ext:
        ref = f"refhost:{ext}"
        graph.add(Node(ref, "refhost", ext, attrs={"named in": "an ExternalName Service"}))
        target = ref
        if port:
            target = f"port:{ref}:{port}"
            graph.add(Node(target, "port", str(port), parent=ref))
        graph.link(rid, target, "routes to")
        return
    # The chart deployed into the route's namespace: an Application, or a
    # HelmChart Ansible installs. Not the app-of-apps, which only syncs others.
    candidates = [(a["id"], a["namespace"]) for a in apps.values() if not a["app_of_apps"]]
    candidates += [(n.id, n.attrs.get("namespace")) for n in graph.nodes.values() if n.kind == "helm" and not n.id.startswith("argo:")]
    for app_id, app_ns in candidates:
        if app_ns == ns:
            target = app_id
            if port:
                target = f"port:{app_id}:{port}"
                graph.add(Node(target, "port", f"{name}:{port}", parent=app_id, attrs={"service": name}))
            graph.link(rid, target, "routes to")
            return
    graph.unresolved_ref(f"{ns}/{name}", "", 0, "a route to a namespace no Application deploys into")


def _values_links(repo, graph, name, app, apps) -> None:
    """From an app's Helm values: volumes it asks for, other apps it talks to,
    and host directories it stores data in."""
    nid = app["id"]
    doc = app["doc"].value
    spec = doc.get("spec", {})
    sources = spec.get("sources") or [spec.get("source")]
    for s in sources:
        if not isinstance(s, CommentedMap):
            continue
        vo = (s.get("helm") or {}).get("valuesObject")
        if isinstance(vo, CommentedMap):
            _walk_values(repo, graph, app, apps, vo, [])


def _walk_values(repo, graph, app, apps, node, path) -> None:
    f = app["file"]
    if isinstance(node, CommentedMap):
        keys = {str(k) for k in node}
        cls_key = next((k for k in ("storageClass", "className", "storageClassName") if k in keys), None)
        if cls_key and "size" in keys:
            start, end = span(repo, f, node)
            # Include the key that holds this block, and the comment above it.
            src = repo.source(f, start - 1, end)
            label = ".".join(path) or "volume"
            did = f"pvc:{app['id'][5:]}:{label}"
            graph.add(Node(did, "disk", f"{label} {node['size']}", parent=app["id"],
                           attrs={"type": "volume from Helm values", "storage class": str(node[cls_key]), "size": str(node["size"])},
                           sources=[src]))
            _storage_class(graph, did, str(node[cls_key]), apps)
        for k, v in node.items():
            if isinstance(v, str):
                _value_string(graph, app, apps, v)
            _walk_values(repo, graph, app, apps, v, path + [str(k)])
    elif isinstance(node, CommentedSeq):
        for v in node:
            if isinstance(v, str):
                _value_string(graph, app, apps, v)
            _walk_values(repo, graph, app, apps, v, path)


def _value_string(graph, app, apps, v: str) -> None:
    # A URL to an in-cluster Service: this app talks to the one in that namespace.
    m = _SVC_URL.search(v)
    if m:
        for other in apps.values():
            if other["namespace"] == m.group(2) and other["id"] != app["id"]:
                graph.link(app["id"], other["id"], "talks to")
    # A path that is a disk's mountpoint on the cluster's host: data lives there.
    if v.startswith("/"):
        for n in list(graph.nodes.values()):
            if n.kind == "disk" and n.attrs.get("mountpoint") == v:
                graph.link(app["id"], n.id, f"stores data in {v}")
