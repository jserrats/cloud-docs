"""The generator against tests/fixture: a small repository in the same shape as
jserrats/cloud, with made-up names, committed into a temporary git repo."""

import shutil
import subprocess
from pathlib import Path

import pytest

from generator.build import build, web_url

FIXTURE = Path(__file__).parent / "fixture"


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    root = tmp_path_factory.mktemp("repo")
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "fixture")
    git("remote", "add", "origin", "git@github.com:o/r.git")
    data = build(root)
    assert data["errors"] == []
    return data


def node(g, id_):
    found = [n for n in g["nodes"] if n["id"] == id_]
    assert found, f"no node {id_}; have {sorted(n['id'] for n in g['nodes'])}"
    return found[0]


def edge(g, source, target, kind=None):
    return any(e["source"] == source and e["target"] == target and (kind is None or e["kind"] == kind) for e in g["edges"])


def comments(n):
    return "\n".join(c for s in n["sources"] for c in s["comments"])


def test_repo_url():
    assert web_url("git@github.com:o/r.git") == "https://github.com/o/r"
    assert web_url("https://github.com/o/r.git") == "https://github.com/o/r"


def test_terraform_for_each_over_tfvars(graph):
    tunnel = node(graph, 'tf:stacks/edge:cloudflare_zero_trust_tunnel_cloudflared.this["lab"]')
    assert tunnel["kind"] == "cloud"
    assert tunnel["parent"] == "tf:stacks/edge"
    # The banner above the resource, and the tfvars entry's own comment.
    assert 'config_src = "cloudflare" is prose, not code.' in comments(tunnel)
    assert "The tunnel for the lab." in comments(tunnel)
    # The config resource is folded into the tunnel, URL and all.
    assert edge(graph, tunnel["id"], "port:host:box:8123", "forwards to")


def test_dns_record_from_nested_for_expression(graph):
    rec = node(graph, "dns:app.example.com:CNAME")
    assert edge(graph, rec["id"], 'tf:stacks/edge:cloudflare_zero_trust_tunnel_cloudflared.this["lab"]', "points to")


def test_config_resources_fold_into_their_owner(graph):
    bucket = node(graph, "tf:stacks/edge:aws_s3_bucket.state")
    assert "Versioning, folded into the bucket." in comments(bucket)
    assert not any(n["id"].endswith("aws_s3_bucket_versioning.state") for n in graph["nodes"])


def test_backend_bucket_matched_as_pattern(graph):
    assert edge(graph, "tf:stacks/edge", "tf:stacks/edge:aws_s3_bucket.state", "keeps state in")


def test_workflow(graph):
    job = "gha:.github/workflows/apply.yml:apply"
    assert edge(graph, job, "tf:stacks/edge", "runs terraform in")
    assert edge(graph, job, "ghenv:deploy", "deploys in")
    assert edge(graph, job, "secret:ssm:/edge/token", "reads")
    secret = node(graph, "secret:ssm:/edge/token")
    assert "Reads the token from SSM." in comments(secret)
    assert secret["attrs"]["created"].startswith("by hand")
    # The module's outputs name the parameter and the environment.
    assert edge(graph, "tf:stacks/edge:module.secrets", "secret:ssm:/edge/token", "grants read on")
    assert edge(graph, "ghenv:deploy", "tf:stacks/edge:module.secrets", "trusted by")
    assert any(u["text"] == "vars.ROLE_ARN" for u in graph["unresolved"])


def test_ansible_vms_and_disks(graph):
    vm = node(graph, "host:vm2")
    assert vm["kind"] == "vm" and vm["parent"] == "host:pve"
    assert "A trailing comment that belongs to vm2." in comments(vm)
    data = node(graph, "disk:vm2:data")
    assert data["attrs"]["mountpoint"] == "/var/lib/longhorn"
    assert edge(graph, "host:vm2", "disk:pve:data", "mounts")
    assert node(graph, "cluster:vm2")["attrs"]["version"] == "v1.0.0+k3s1"
    assert edge(graph, "host:vm2", "net:lan", "routes (gw)")
    assert "pve.lan" in comments(node(graph, "host:pve"))


def test_compose(graph):
    ctr = node(graph, "ctr:vm1/web/web")
    assert ctr["parent"] == "stack:vm1/web"
    assert node(graph, "stack:vm1/web")["parent"] == "host:vm1"
    assert "The web app." in comments(ctr)
    assert edge(graph, ctr["id"], "vol:vm1/web/html", "mounts")
    assert edge(graph, "vol:vm1/web/html", "disk:vm1:data", "stored on")
    assert edge(graph, ctr["id"], "disk:pve:data", "mounts /srv/data/www")
    assert edge(graph, ctr["id"], "net:vm1:proxy", "joins")
    # DOMAIN and MEDIA come from docs/homelab.yaml: the closest directory wins.
    assert node(graph, "route:vm1:web.example.com")
    web = node(graph, "stack:vm1/web")
    assert web["attrs"]["documented variables"] == {"DOMAIN": "example.com", "MEDIA": "/srv/data/media"}
    assert "Set on the web stack in the deploy tool." in comments(web)
    assert edge(graph, ctr["id"], "disk:pve:data", "mounts /srv/data/media")
    assert not any(u["text"] in ("${DOMAIN}", "${MEDIA}") for u in graph["unresolved"])
    # The proxy stack's .env resolves its route, over any documented value, and
    # its empty token is a secret.
    traefik = "ctr:vm1/proxy/traefik"
    assert edge(graph, traefik, "route:vm1:traefik.vm1.example.com", "serves")
    assert edge(graph, traefik, "secret:env:vm1/proxy/CF_API_TOKEN", "reads")
    assert "Set in the deploy tool." in comments(node(graph, "secret:env:vm1/proxy/CF_API_TOKEN"))
    assert node(graph, "port:ctr:vm1/proxy/traefik:443")
    # The file provider's route.
    assert edge(graph, "route:vm1:pve.vm1.example.com", "port:host:pve:8006", "routes to")


def test_argocd(graph):
    app = node(graph, "argo:app1")
    assert app["kind"] == "helm" and app["parent"] == "cluster:vm2"
    assert "App one, a local chart." in comments(app)
    assert edge(graph, "argo:root", "argo:app1", "syncs")
    assert edge(graph, "pvc:app1:app1-data", "argo:longhorn", "stored on")
    assert edge(graph, "pvc:traefik:persistence", "argo:longhorn", "stored on")
    assert edge(graph, "argo:longhorn", "disk:vm2:data")
    assert edge(graph, "argo:app1", "disk:pve:data", "mounts /srv/data/books")

    secret = node(graph, "secret:infisical:proj/prod/app1")
    assert edge(graph, "argo:app1", secret["id"], "reads")
    assert edge(graph, secret["id"], "argo:longhorn", "synced by")

    route = node(graph, "route:app1.example.com")
    assert route["attrs"]["hosts"] == ["app1.k.example.com", "app1.example.com"]
    assert route["attrs"]["middlewares"] == ["lan"]
    assert edge(graph, route["id"], "port:argo:app1:8080", "routes to")
    assert edge(graph, "argo:traefik", route["id"], "serves")

    # An ExternalName route, and the public record with the same name.
    assert edge(graph, "route:app.example.com", "port:host:box:8123", "routes to")
    assert edge(graph, "dns:app.example.com:CNAME", "route:app.example.com", "same name as")


def test_links_are_pinned_to_the_commit(graph):
    assert graph["repo"] == "https://github.com/o/r"
    assert len(graph["sha"]) == 40
    for n in graph["nodes"]:
        for s in n["sources"]:
            assert (FIXTURE / s["file"]).is_file(), s
            assert 1 <= s["start"] <= s["end"]


def test_homelab(graph):
    lan = node(graph, "net:lan")
    assert lan["attrs"]["vlan id"] == 10
    assert "The main LAN." in comments(lan)
    # The subnet the gateway role routes is the same network.
    assert not any(n["id"] == "net:cidr:10.1.0.0/24" for n in graph["nodes"])
    assert edge(graph, "host:vm2", "net:lan")
    assert node(graph, "net:things")["attrs"]["subnet"].startswith("not recorded")
    assert lan["attrs"]["ipv6 subnet"] == "fd00:1::/64" and lan["attrs"]["dhcp"] == "server"
    assert edge(graph, "host:gw", "net:lan", "routes")
    assert any(u["text"] == "nowhere" for u in graph["unresolved"])

    pve = node(graph, "host:pve")
    assert pve["attrs"]["physical"] is True and pve["attrs"]["model"] == "Mini PC"
    assert "The hypervisor box." in comments(pve)
    assert edge(graph, "host:pve", "net:lan", "on, as 10.1.0.2")
    assert edge(graph, "host:vm1", "net:lan", "on, as 10.1.0.11")

    # Referenced hosts become the machines they name, ports and all.
    assert not any(n["kind"] == "refhost" and n["label"] in ("pve.lan", "box.lan") for n in graph["nodes"])
    assert edge(graph, "route:vm1:pve.vm1.example.com", "port:host:pve:8006", "routes to")
    assert node(graph, "port:host:pve:8006")["label"] == "8006 Proxmox UI"
    assert edge(graph, "route:app.example.com", "port:host:box:8123", "routes to")
    assert edge(graph, 'tf:stacks/edge:cloudflare_zero_trust_tunnel_cloudflared.this["lab"]', "port:host:box:8123")
    assert node(graph, "host:box")["parent"] is None

    # The pool, its disks, and the shared directory on it.
    assert edge(graph, "disk:pve:pool:data", "disk:pve:hw:a", "built from")
    assert edge(graph, "disk:pve:data", "disk:pve:pool:data", "on pool")
