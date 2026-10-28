# cloud-docs

An interactive map of [jserrats/cloud](https://github.com/jserrats/cloud), drawn from its code
on every commit to `main`. It shows hosts, VMs, disks, networks, compose stacks and containers,
Helm charts, Traefik routes and ports, DNS records, buckets, cloud services, secrets and GitHub
Actions, and how they connect. Click any item to see the comments written next to it and links to
the exact lines that declare it.

The app holds no information of its own. Every name, address and relation comes from the
checkout; the code here only knows how Terraform, Ansible, compose files, ArgoCD manifests and
GitHub workflows are written.

## How it runs

```
pod
  git-sync   pulls the repository's branch every 60s into /repo, behind the symlink /repo/current
  app        sees /repo/current move, rebuilds graph.json from it, serves the UI and graph.json on :8080
```

A failed build keeps serving the previous map. Open pages reload when the commit changes. Every
source link points at the commit that was parsed, so the lines always match.

## What it reads

| Source | Becomes |
|---|---|
| Terraform stacks (any directory with a `backend` block) | one node per resource instance, under its stack and provider. Variables come from defaults and `terraform.tfvars`. `locals`, `for_each` and module calls are evaluated (`generator/hcleval.py`), so `for_each` over a tfvars map gives one node per entry, with that entry's comments. Resources that only configure another one, such as a bucket's versioning or a role's policy, are folded into it. |
| `.github/workflows/*.yml` | workflows and jobs; the environments jobs deploy in; the Terraform stacks in a job's matrix; the SSM parameters a step reads |
| `ansible/` | hosts and groups from the inventory; VMs, their disks and virtiofs shares from the Proxmox host's `host_vars`; roles from the playbooks; what roles declare (a k3s cluster, a HelmChart, a Secret, subnets, docker networks) |
| `docker/<host>/<stack>/` | compose stacks on that host: containers, published ports, volumes, networks, Traefik routes from labels and from the file provider. `${VAR}` is resolved from the stack's `.env`, then from the `variables` section of `docs/homelab.yaml` (values set outside the repo, keyed by directory, closest wins). |
| `argocd/` | Applications as Helm charts on the cluster. From the local ingress chart: routes, middlewares and ExternalName targets. From all charts: InfisicalSecrets, PVCs, hostPaths, and volumes in Helm values. Deployment internals are out of scope. |
| `docs/homelab.yaml` | networks, physical machines, IPs, hardware, ZFS pools and services on the hardware, the facts no code declares. Read last: a machine absorbs any referenced host named by its hostname or IP (`kore.iot` becomes the machine `kore`, ports and routes included), and a network absorbs the subnet node an Ansible role lists. Values left null show as "not recorded". The shape is `docs/homelab.schema.json` in that repository. |

Then `generator/resolve.py` links items that only meet by name: a public DNS record and the route
with the same host name, an SSM parameter and the module that grants it, a stack's backend and its
state bucket, a GitHub environment and the module whose trust policy names it.

### Unresolved

The UI lists anything the code names but does not declare. This covers GitHub repository
variables, compose variables with no value in the repo, SSM parameters that no resource creates,
and hosts that appear only in a URL or an ExternalName. These values live in a dashboard, a
secrets manager or a shell. They are not in the repo, so the map cannot show them.

## Developing

```sh
./dev.sh ../cloud            # http://localhost:8080, rebuilt on every edit to either repo
uv run pytest                # generator, against tests/fixture
uv run python -m generator.build ../cloud /tmp/graph.json   # one build, to a file
```

`dev.sh` watches the checkout for new commits and uncommitted edits, and `web/` for UI changes.
Open pages reload themselves within a few seconds.

`tests/fixture/` is a small repository with the same shape as `cloud`, using made-up names. It is
copied into a temporary git repository for each run.

After changing dependencies in `pyproject.toml`, run
`uv export --no-dev --no-emit-project --format requirements-txt -o requirements.txt`. The image
installs from that file, with hashes, and CI fails if it drifts from `uv.lock`.

## Deploying

CI publishes `ghcr.io/jserrats/cloud-docs:sha-<short sha>` on every push to `main`. The chart in
`chart/` runs the pod. It takes:

- `repo.url`: the SSH URL to map.
- `image.tag`: the `sha-<short sha>` tag to run.
- `infisical`: the shape of `argocd/cluster.yaml`, so that file can be passed in as a value file.

With `infisical.identityId` set, an InfisicalSecret syncs the deploy key from the folder
`infisical.secretsPath` (`/docs`), key `SSH_KEY`. Without it, create the Secret yourself.

On ganymede it is the `docs` Application in `jserrats/cloud` (`argocd/apps/docs.yaml`), pinned to
a commit of this repository and the image built from it. Upgrade both together.
