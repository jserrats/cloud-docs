"""docs/homelab.yaml: networks, physical machines and their addresses, as the
repository documents them for anything that has no code of its own.

It runs last, because what it adds mostly joins things other extractors found:

- a machine's hostname or IP that some URL or ExternalName names (kore.iot,
  10.0.0.1) turns that referenced host into the machine, ports and all;
- a network whose subnet an Ansible role lists becomes one node with it;
- a pool's mountpoint above a shared directory's path ties the share to the
  pool, and the pool to its disks;
- a network's `router` ties it to the machine that routes it.

A value the file leaves null is shown as not recorded rather than left out,
so the gap is visible.
"""

from __future__ import annotations

from ..model import Graph, Node
from ..source import Repo
from ..yamlsrc import entries, entry, load, plain

FILE = "docs/homelab.yaml"
UNKNOWN = f"not recorded (null in {FILE})"


def extract(repo: Repo, graph: Graph) -> None:
    if not repo.exists(FILE):
        return
    docs = load(repo, FILE)
    if not docs or not isinstance(docs[0].value, dict):
        graph.unresolved_ref(FILE, FILE, 1, "not a YAML mapping; skipped")
        return
    doc = docs[0]
    networks = entry(repo, FILE, doc.value, "networks", doc.end)
    nets = entries(repo, FILE, networks.value, networks.end) if networks else []
    for n in nets:
        _network(repo, graph, n)
    machines = entry(repo, FILE, doc.value, "machines", doc.end)
    hosts = {}
    for m in entries(repo, FILE, machines.value, machines.end) if machines else []:
        hosts[str(m.key)] = _machine(repo, graph, m)
    # Routers last: they are machines, which come after the networks.
    for n in nets:
        router = (plain(n.value) or {}).get("router")
        if router in hosts:
            graph.link(hosts[router], f"net:{n.key}", "routes")
        elif router:
            graph.unresolved_ref(router, FILE, n.start, f"the router of {n.key}, a machine homelab.yaml does not list")


def _shown(v):
    return UNKNOWN if v is None else v


def _network(repo: Repo, graph: Graph, e) -> None:
    name, v = str(e.key), plain(e.value) or {}
    nid = f"net:{name}"
    kind = v.get("type", "vlan")
    label = f"{name} {'VLAN' if kind == 'vlan' else 'network'}"
    if v.get("subnet"):
        label += f" ({v['subnet']})"
    attrs = {"type": kind, "subnet": _shown(v.get("subnet"))}
    if v.get("ipv6_subnet"):
        attrs["ipv6 subnet"] = v["ipv6_subnet"]
    if kind == "vlan":
        attrs.update({"vlan id": _shown(v.get("vlan_id")), "gateway": _shown(v.get("gateway")),
                      "dns suffix": _shown(v.get("dns_suffix"))})
    for k in ("router", "dhcp", "description"):
        if v.get(k):
            attrs[k] = v[k]
    graph.add(Node(nid, "network", label, attrs=attrs, sources=[repo.source(FILE, e.start, e.end)]))
    # The same subnet, as an Ansible role listed it.
    if v.get("subnet") and f"net:cidr:{v['subnet']}" in graph.nodes:
        graph.merge(f"net:cidr:{v['subnet']}", nid)


def _machine(repo: Repo, graph: Graph, e) -> str:
    key, v = str(e.key), plain(e.value) or {}
    hid = f"host:{v.get('inventory', key)}"
    kind = "vm" if v.get("type") == "vm" else "host"
    attrs = {}
    if v.get("type") == "physical":
        attrs["physical"] = True
    if v.get("inventory"):
        attrs["inventory host"] = v["inventory"]
    for k in ("role", "model", "cpu", "memory", "os", "description"):
        if v.get(k):
            attrs[k] = v[k]
    if v.get("nics"):
        attrs["nics"] = [" ".join(str(x) for x in (n.get("name"), n.get("model") and f"({n['model']})",
                                                    n.get("bridge") and f"bridged as {n['bridge']}") if x)
                         for n in v["nics"]]
    addresses = []
    for i in v.get("interfaces", []):
        parts = [p for p in (i.get("hostname"), i.get("ip") or "IP not recorded", i.get("assignment")) if p]
        addresses.append(f"{i['network']}: " + ", ".join(parts))
    if addresses:
        attrs["addresses"] = addresses
    graph.add(Node(hid, kind, key, attrs=attrs, sources=[repo.source(FILE, e.start, e.end)]))
    node = graph.nodes[hid]
    if node.kind == "host" and kind == "vm":
        node.kind = "vm"
    # The machine's own name over its inventory alias (kore, not ha).
    node.label = key

    # Interfaces: which network the machine is on, and the names other code
    # reaches it by.
    for i in v.get("interfaces", []):
        net = f"net:{i['network']}"
        if net not in graph.nodes:
            graph.unresolved_ref(i["network"], FILE, e.start, f"{key} is on a network homelab.yaml does not list")
            continue
        graph.link(hid, net, f"on, as {i['ip']}" if i.get("ip") else "on")
        for name in (i.get("hostname"), i.get("ip")):
            if name and f"refhost:{name}" in graph.nodes:
                _absorb(graph, f"refhost:{name}", hid)

    # Services on the hardware itself, merged with the ports routes already
    # point at.
    for s in v.get("services", []):
        pid = f"port:{hid}:{s['port']}"
        label = f"{s['port']} {s['name']}"
        if pid in graph.nodes:
            graph.nodes[pid].label = label
        graph.add(Node(pid, "port", label, parent=hid,
                       attrs={k: s[k] for k in ("name", "protocol", "network", "description") if s.get(k)},
                       sources=[repo.source(FILE, e.start, e.end, inner=False, leading=False)]))

    _disks(repo, graph, hid, key, v, e)
    return hid


def _absorb(graph: Graph, ref_id: str, hid: str) -> None:
    """A referenced host is this machine: its ports become the machine's."""
    for port in [n for n in graph.nodes.values() if n.parent == ref_id and n.kind == "port"]:
        number = port.id.rsplit(":", 1)[1]
        new = f"port:{hid}:{number}"
        if new in graph.nodes:
            graph.merge(port.id, new)
        else:
            graph.nodes.pop(port.id)
            old_id, port.id, port.parent = port.id, new, hid
            graph.nodes[new] = port
            for ed in graph.edges:
                if ed.source == old_id:
                    ed.source = new
                if ed.target == old_id:
                    ed.target = new
    graph.nodes[hid].attrs.setdefault("also named", []).append(graph.nodes[ref_id].label)
    graph.nodes[ref_id].attrs.pop("named in", None)
    graph.merge(ref_id, hid)


def _disks(repo: Repo, graph: Graph, hid: str, key: str, v: dict, e) -> None:
    disks = entry(repo, FILE, e.value, "disks", e.end)
    for d in entries(repo, FILE, disks.value, disks.end) if disks else []:
        dv = plain(d.value)
        attrs = {k: dv[k] for k in ("model", "serial", "size", "use") if dv.get(k)}
        label = f"{dv['name']} {dv.get('size', '')}".strip()
        graph.add(Node(f"disk:{hid[5:]}:hw:{dv['name']}", "disk", label, parent=hid,
                       attrs={"type": "physical disk", **attrs}, sources=[repo.source(FILE, d.start, d.end)]))
    pools = entry(repo, FILE, e.value, "pools", e.end)
    for p in entries(repo, FILE, pools.value, pools.end) if pools else []:
        pv = plain(p.value)
        pid = f"disk:{hid[5:]}:pool:{pv['name']}"
        attrs = {"type": pv.get("type"), "mountpoint": pv.get("mountpoint")}
        attrs.update({k: str(x) for k, x in (pv.get("options") or {}).items()})
        graph.add(Node(pid, "disk", f"{pv['name']} ({pv.get('type')})", parent=hid,
                       attrs=attrs, sources=[repo.source(FILE, p.start, p.end)]))
        for d in pv.get("disks", []):
            graph.link(pid, f"disk:{hid[5:]}:hw:{d}", "built from")
        # Directories on this host under the pool's mountpoint live on it.
        mount = (pv.get("mountpoint") or "").rstrip("/")
        for n in list(graph.nodes.values()):
            path = n.attrs.get("path")
            if mount and n.kind == "disk" and n.parent == hid and isinstance(path, str) and path.startswith(mount + "/"):
                graph.link(n.id, pid, "on pool")
