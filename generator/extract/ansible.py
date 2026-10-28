"""Ansible: hosts from the inventory, VMs and their disks from the Proxmox
host's vars, the roles each host gets from the playbooks, and what those roles
declare in their defaults and templates (a k3s cluster, a HelmChart, a Secret,
the subnets a gateway serves, docker networks)."""

from __future__ import annotations

import ipaddress
import posixpath
import re

from ..model import Graph, Node
from ..source import Repo
from ..yamlsrc import TPL, entries, entry, load, plain

ROOT = "ansible"


def extract(repo: Repo, graph: Graph) -> None:
    inv = f"{ROOT}/inventory.yml"
    if not repo.exists(inv):
        return
    groups: dict[str, list[str]] = {}
    doc = load(repo, inv)[0]
    _walk_inventory(repo, graph, inv, doc.value, doc.end, groups, [])

    host_vars = {}
    for f in repo.files(f"{ROOT}/host_vars/*.yml"):
        host = posixpath.basename(f)[:-4]
        docs = load(repo, f)
        if docs:
            host_vars[host] = (f, docs[0])
    group_vars = {}
    for f in repo.files(f"{ROOT}/group_vars/*.yml"):
        docs = load(repo, f)
        if docs:
            group_vars[posixpath.basename(f)[:-4]] = (f, docs[0])

    for host, (f, doc) in host_vars.items():
        _proxmox(repo, graph, host, f, doc, groups, group_vars)

    roles_by_host = _playbooks(repo, graph, groups)
    for host, roles in roles_by_host.items():
        for role in roles:
            _role(repo, graph, host, role)

    # Docker networks every host in a group creates.
    for group, (f, doc) in group_vars.items():
        nets = entry(repo, f, doc.value, "docker_networks", doc.end)
        if nets is None:
            continue
        for host in groups.get(group, []):
            for name in plain(nets.value):
                graph.add(
                    Node(f"net:{host}:{name}", "network", name, parent=f"host:{host}", attrs={"type": "docker network"},
                         sources=[repo.source(f, nets.start, nets.end)])
                )


def _walk_inventory(repo, graph, f, node, end, groups, path):
    for e in entries(repo, f, node, end):
        if e.key == "children":
            _walk_inventory(repo, graph, f, e.value, e.end, groups, path)
        elif e.key == "hosts":
            group, group_entry = path[-1] if path else ("all", None)
            for h in entries(repo, f, e.value, e.end):
                name = str(h.key)
                attrs = {"inventory groups": [group]}
                if isinstance(h.value, dict):
                    attrs.update({k: v for k, v in plain(h.value).items()})
                sources = [repo.source(f, h.start, h.end)]
                if group_entry:
                    # The group's own comment says what every host in it is.
                    sources.append(repo.source(f, group_entry.start, group_entry.start, inner=False))
                existing = graph.nodes.get(f"host:{name}")
                if existing:
                    existing.attrs["inventory groups"].append(group)
                graph.add(Node(f"host:{name}", "host", name, attrs=attrs, sources=sources))
                groups.setdefault(group, []).append(name)
        elif isinstance(e.value, dict):
            _walk_inventory(repo, graph, f, e.value, e.end, groups, path + [(str(e.key), e)])


def _proxmox(repo, graph, host, f, doc, groups, group_vars) -> None:
    """VMs, their disks and the directories shared into them."""
    maps = entry(repo, f, doc.value, "proxmox_dir_mappings", doc.end)
    for m in entries(repo, f, maps.value, maps.end) if maps else []:
        v = plain(m.value)
        graph.add(
            Node(f"disk:{host}:{v['id']}", "disk", f"{v['path']} (virtiofs {v['id']})", parent=f"host:{host}",
                 attrs={"path": v["path"], "type": "virtiofs directory mapping"},
                 sources=[repo.source(f, m.start, m.end), repo.source(f, maps.start, maps.start, inner=False)])
        )
    vms = entry(repo, f, doc.value, "proxmox_vms", doc.end)
    for vm in entries(repo, f, vms.value, vms.end) if vms else []:
        v = plain(vm.value)
        name = v["name"]
        vid = f"host:{name}"
        attrs = {k: v[k] for k in ("vmid", "cores", "memory", "balloon", "mac") if k in v}
        graph.add(Node(vid, "vm", name, parent=f"host:{host}", attrs=attrs,
                       sources=[repo.source(f, vm.start, vm.end)]))
        vm_groups = [g for g, hs in groups.items() if name in hs]
        if "disk_size" in v:
            de = entry(repo, f, vm.value, "disk_size", vm.end)
            graph.add(Node(f"disk:{name}:os", "disk", f"OS disk {v['disk_size']}", parent=vid,
                           attrs={"size": v["disk_size"]}, sources=[repo.source(f, de.start, de.end)]))
        if "data_disk_gb" in v:
            de = entry(repo, f, vm.value, "data_disk_gb", vm.end)
            attrs = {"size": f"{v['data_disk_gb']}G"}
            sources = [repo.source(f, de.start, de.end)]
            for g in vm_groups:
                if g in group_vars:
                    gf, gdoc = group_vars[g]
                    for key in ("data_disk_mountpoint", "data_disk_label"):
                        ke = entry(repo, gf, gdoc.value, key, gdoc.end)
                        if ke:
                            attrs[key.removeprefix("data_disk_")] = str(ke.value)
                            sources.append(repo.source(gf, ke.start, ke.end))
            graph.add(Node(f"disk:{name}:data", "disk", f"data disk {v['data_disk_gb']}G", parent=vid,
                           attrs=attrs, sources=sources))
        for d in v.get("shared_dirs", []):
            graph.link(vid, f"disk:{host}:{d}", "mounts")


def _playbooks(repo, graph, groups) -> dict[str, list[str]]:
    roles_by_host: dict[str, list[str]] = {}
    for f in repo.files(f"{ROOT}/playbooks/*.yml"):
        docs = load(repo, f)
        if not docs or not isinstance(docs[0].value, list):
            continue
        for play in entries(repo, f, docs[0].value, docs[0].end):
            p = plain(play.value)
            if not isinstance(p, dict) or "hosts" not in p:
                continue
            hosts = []
            for pat in re.split(r"[:,]", str(p["hosts"])):
                hosts += groups.get(pat, [pat] if f"host:{pat}" in graph.nodes else [])
            roles = [r if isinstance(r, str) else r.get("role") for r in p.get("roles", [])]
            for h in hosts:
                node = graph.nodes.get(f"host:{h}")
                if node is None:
                    continue
                node.sources.append(repo.source(f, play.start, play.end, inner=False))
                if roles:
                    node.attrs.setdefault("roles", [])
                    node.attrs["roles"] += [r for r in roles if r not in node.attrs["roles"]]
                    roles_by_host.setdefault(h, []).extend(roles)
                    node.attrs.setdefault("playbooks", []).append(f)
    return roles_by_host


def _role(repo: Repo, graph: Graph, host: str, role: str) -> None:
    base = f"{ROOT}/roles/{role}"
    defaults = f"{base}/defaults/main.yml"
    dv = load(repo, defaults)[0] if repo.exists(defaults) and load(repo, defaults) else None
    values = plain(dv.value) if dv else {}

    # A role that installs k3s makes its host a cluster.
    if role == "k3s" and "k3s_version" in values:
        e = entry(repo, defaults, dv.value, "k3s_version", dv.end)
        graph.add(Node(f"cluster:{host}", "cluster", f"k3s on {host}", parent=f"host:{host}",
                       attrs={"version": values.get("k3s_version"), "disabled components": values.get("k3s_disable", [])},
                       sources=[repo.source(defaults, e.start, e.end)]))

    # HelmChart manifests a role writes for k3s's Helm controller.
    for t in repo.files(f"{base}/templates/*"):
        for d in load(repo, t, template=True):
            v = plain(d.value)
            if isinstance(v, dict) and v.get("kind") == "HelmChart":
                spec = v.get("spec", {})
                version = spec.get("version")
                if isinstance(version, str) and TPL in version:
                    var = re.search(r"\{\{\s*(\w+)\s*\}\}", "\n".join(repo.lines(t)))
                    version = values.get(var.group(1)) if var else version
                src = repo.source(t, d.start, d.end)
                src.comments = repo.header(t) + src.comments
                graph.add(Node(f"helm:{host}:{v['metadata']['name']}", "helm", v["metadata"]["name"],
                               parent=f"cluster:{host}",
                               attrs={"chart": spec.get("chart"), "repo": spec.get("repo"), "version": version,
                                      "namespace": spec.get("targetNamespace"), "installed by": f"ansible role {role}"},
                               sources=[src]))

    if not dv:
        return
    for e in entries(repo, defaults, dv.value, dv.end):
        v = plain(e.value)
        # A Kubernetes Secret the role creates.
        if isinstance(v, dict) and v.get("kind") == "Secret":
            md = v.get("metadata", {})
            sid = f"secret:k8s:{md.get('namespace')}/{md.get('name')}"
            graph.add(Node(sid, "secret", f"{md.get('namespace')}/{md.get('name')}", parent=f"cluster:{host}",
                           attrs={"store": "Kubernetes Secret", "keys": sorted(v.get("stringData", v.get("data", {})).keys()),
                                  "created by": f"ansible role {role}"},
                           sources=[repo.source(defaults, e.start, e.end)]))
        # A list of subnets: what a gateway role routes for.
        if isinstance(v, list) and v and all(_is_cidr(x) for x in v):
            for cidr in v:
                nid = f"net:cidr:{cidr}"
                graph.add(Node(nid, "network", cidr, attrs={"type": "subnet"}, sources=[repo.source(defaults, e.start, e.end)]))
                graph.link(f"host:{host}", nid, f"routes ({role})")


def _is_cidr(x) -> bool:
    try:
        return isinstance(x, str) and "/" in x and bool(ipaddress.ip_network(x, strict=False))
    except ValueError:
        return False
