"""YAML with line ranges, for compose files, Ansible and ArgoCD manifests.

ruamel.yaml reports where each key and list item starts; the end of an entry is
the last code line before the next sibling starts. Helm and Jinja templates are
made loadable first by blanking the lines that are only template directives and
replacing inline expressions with a placeholder, which keeps every line number.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .source import Repo

TPL = "__template__"

_DIRECTIVE_LINE = re.compile(r"^\s*(\{\{-?[^}]*-?\}\}|\{%-?.*?-?%\}|\{#.*?#\})\s*$")
_INLINE = re.compile(r"\{\{-?.*?-?\}\}|\{%-?.*?-?%\}")
_COMMENT_OR_BLANK = re.compile(r"^\s*(#.*)?$")


@dataclass
class Entry:
    key: object      # mapping key, or list index
    value: object
    start: int       # 1-based
    end: int


@dataclass
class Doc:
    value: object
    start: int
    end: int


def detemplate(text: str) -> str:
    out = []
    for line in text.splitlines():
        if _DIRECTIVE_LINE.match(line):
            out.append("")
        else:
            out.append(_INLINE.sub(lambda m: _placeholder(m.group(0)), line))
    return "\n".join(out) + "\n"


def _placeholder(expr: str) -> str:
    # Keep a helper call's literal argument readable: ingress.hosts "calibre".
    m = re.search(r'include\s+"([\w.-]+)"\s+\(list\s+\$\s+"([^"]+)"\)', expr)
    if m:
        return f"{TPL}:{m.group(1)}:{m.group(2)}"
    m = re.search(r"\.Values\.([\w.]+)", expr)
    if m:
        return f"{TPL}:values:{m.group(1)}"
    return TPL


def load(repo: Repo, rel: str, template: bool = False) -> list[Doc]:
    text = repo.text(rel)
    if template:
        text = detemplate(text)
    lines = text.splitlines()
    # Split on document markers by hand, so each document keeps its own range.
    starts = [0] + [i + 1 for i, l in enumerate(lines) if l.strip() == "---"]
    ends = [i for i, l in enumerate(lines) if l.strip() == "---"] + [len(lines)]
    docs = []
    yaml = YAML(typ="rt")
    for s, e in zip(starts, ends):
        chunk = "\n".join([""] * s + lines[s:e]) + "\n"
        try:
            value = yaml.load(io.StringIO(chunk))
        except Exception:
            continue
        if value is None:
            continue
        first = _first_code_line(lines, s, e)
        docs.append(Doc(value, first, _last_code_line(lines, e)))
    return docs


def entries(repo: Repo, rel: str, node, end: int) -> list[Entry]:
    """The keys of a mapping, or items of a list, with their line ranges."""
    lines = repo.lines(rel)
    out: list[Entry] = []
    if isinstance(node, CommentedMap):
        keys = list(node.keys())
        starts = [node.lc.key(k)[0] + 1 for k in keys]
    elif isinstance(node, CommentedSeq):
        keys = list(range(len(node)))
        starts = [node.lc.item(i)[0] + 1 for i in keys]
    else:
        return out
    for i, k in enumerate(keys):
        if i + 1 < len(starts):
            last = _last_code_line(lines, starts[i + 1] - 1)
        else:
            # The last entry also owns a comment block written right after it,
            # with no blank line between: there is no next sibling to own it.
            last = _last_code_line(lines, end)
            while last < len(lines) and _COMMENT_OR_BLANK.match(lines[last]) and lines[last].strip():
                last += 1
        out.append(Entry(k, node[k], starts[i], max(starts[i], last)))
    return out


def entry(repo: Repo, rel: str, node, key, end: int) -> Entry | None:
    for e in entries(repo, rel, node, end):
        if e.key == key:
            return e
    return None


def _first_code_line(lines: list[str], s: int, e: int) -> int:
    for i in range(s, e):
        if not _COMMENT_OR_BLANK.match(lines[i]):
            return i + 1
    return s + 1


def _last_code_line(lines: list[str], end: int) -> int:
    """end is 1-based; walk back over blank and comment lines."""
    i = min(end, len(lines))
    while i > 1 and _COMMENT_OR_BLANK.match(lines[i - 1]):
        i -= 1
    return i


def plain(v):
    """ruamel's round-trip types as plain python."""
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    return str(v)


def span(repo: Repo, rel: str, node) -> tuple[int, int]:
    """The line range of a mapping or list: from its first line to the last
    line indented at least as deep."""
    lines = repo.lines(rel)
    start, col = node.lc.line, node.lc.col
    end = start
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent < col or (indent == col and not line.lstrip().startswith("-") and isinstance(node, CommentedSeq)):
            break
        end = i
    return start + 1, end + 1
