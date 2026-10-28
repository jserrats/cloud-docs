"""GitHub Actions: workflows, their jobs, the environments jobs deploy in, the
Terraform stacks in a job's matrix, and the SSM parameters a step reads."""

from __future__ import annotations

import re

from ..model import Graph, Node
from ..source import Repo
from ..yamlsrc import entries, entry, load, plain

_VARS = re.compile(r"\$\{\{\s*vars\.(\w+)\s*\}\}")
_SECRETS = re.compile(r"\$\{\{\s*secrets\.(\w+)\s*\}\}")


def extract(repo: Repo, graph: Graph) -> None:
    files = repo.files(".github/workflows/*.yml") + repo.files(".github/workflows/*.yaml")
    if not files:
        return
    graph.add(Node("provider:github", "provider", "GitHub"))
    for f in files:
        _workflow(repo, graph, f)


def _workflow(repo: Repo, graph: Graph, f: str) -> None:
    docs = load(repo, f)
    if not docs:
        return
    doc = docs[0]
    wf = doc.value
    wid = f"gha:{f}"
    on = plain(wf.get("on", wf.get(True, {})))
    triggers = list(on.keys()) if isinstance(on, dict) else on if isinstance(on, list) else [on]
    attrs = {"triggers": [str(t) for t in triggers]}
    for trig in ("push", "pull_request"):
        if isinstance(on, dict) and isinstance(on.get(trig), dict):
            for k in ("branches", "paths"):
                if k in on[trig]:
                    attrs[f"{trig} {k}"] = on[trig][k]
    src = repo.source(f, 1, len(repo.lines(f)), inner=False, leading=False)
    src.comments = repo.header(f)
    graph.add(Node(wid, "workflow", str(wf.get("name", f.rsplit("/", 1)[-1])), parent="provider:github", attrs=attrs, sources=[src]))

    jobs = entry(repo, f, wf, "jobs", doc.end)
    if jobs is None:
        return
    for j in entries(repo, f, jobs.value, jobs.end):
        _job(repo, graph, f, wid, j)

    for var in sorted(set(_VARS.findall(repo.text(f)))):
        line = next(i for i, l in enumerate(repo.lines(f), 1) if f"vars.{var}" in l)
        graph.unresolved_ref(
            f"vars.{var}", f, line, "a GitHub repository variable: set in the repository settings, not in code"
        )


def _job(repo: Repo, graph: Graph, f: str, wid: str, j) -> None:
    job = j.value
    jid = f"{wid}:{j.key}"
    attrs = {"runs-on": plain(job.get("runs-on"))}
    for k in ("if", "needs"):
        if k in job:
            attrs[k] = plain(job[k])
    strategy = plain(job.get("strategy") or {})
    matrix = strategy.get("matrix") or {}
    if matrix:
        attrs["matrix"] = matrix
    steps = plain(job.get("steps") or [])
    uses = [s["uses"].split("@")[0] for s in steps if isinstance(s, dict) and "uses" in s]
    if uses:
        attrs["actions"] = uses
    name = str(job.get("name", j.key))
    # The job's own comments: those above its steps. Each step's are shown on
    # what the step touches.
    steps_entry = entry(repo, f, job, "steps", j.end)
    src = repo.source(f, j.start, (steps_entry.start - 1) if steps_entry else j.end)
    src.end = j.end
    graph.add(Node(jid, "job", j.key if "${{" in name else name, parent=wid, attrs=attrs, sources=[src]))

    env = job.get("environment")
    if env:
        name = env if isinstance(env, str) else env.get("name")
        eid = f"ghenv:{name}"
        graph.add(Node(eid, "environment", str(name), parent="provider:github", attrs={"declared by": "jobs that name it"}))
        graph.link(jid, eid, "deploys in")

    # A matrix value that is a Terraform stack directory: the job runs on it.
    for values in matrix.values():
        for v in values if isinstance(values, list) else []:
            if isinstance(v, str):
                graph.link(jid, f"tf:{v}", "runs terraform in")

    # Steps that read an SSM parameter by name. The step, with its comments,
    # is where the parameter is declared as far as this repo goes.
    for st in entries(repo, f, steps_entry.value, steps_entry.end) if steps_entry else []:
        s = plain(st.value)
        if not isinstance(s, dict) or "ssm get-parameter" not in s.get("run", ""):
            continue
        for v in (s.get("env") or {}).values():
            if isinstance(v, str) and v.startswith("/"):
                sid = f"secret:ssm:{v}"
                graph.add(
                    Node(sid, "secret", v, attrs={"store": "AWS SSM Parameter Store"}, sources=[repo.source(f, st.start, st.end)])
                )
                graph.link(jid, sid, "reads")

    # GitHub Actions secrets the job uses.
    for line_no in range(j.start, j.end + 1):
        for name in _SECRETS.findall(repo.lines(f)[line_no - 1]):
            if name == "GITHUB_TOKEN":
                continue
            sid = f"secret:gha:{name}"
            graph.add(Node(sid, "secret", name, parent="provider:github", attrs={"store": "GitHub Actions secrets"}))
            graph.link(jid, sid, "reads")
