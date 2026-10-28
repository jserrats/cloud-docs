"""Reading the checkout: files, line ranges, and the comments that belong to them.

Comments are found by position rather than by any parser, so the same rules
apply to HCL, YAML, compose files, .env files and Helm templates:

- the leading block: the comment lines directly above an item's first line,
  up to the first blank or code line;
- the inner comments: every comment line inside the item's range.

Comment lines that are commented-out code (an example tfvars entry, a disabled
port mapping) are dropped, and so are banner rules like `# -----`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import Source

_COMMENT = re.compile(r"^\s*(#|//)\s?(.*)$")
_RULE = re.compile(r"^[-=#*~_\s]{3,}$")
# What commented-out code looks like in this repo's languages. Each pattern
# must match the whole line: prose that quotes code (`config_src = "x" makes
# ...`) is a comment, not code.
_CODE = re.compile(
    r"""^\s*(
        [\w."/-]+\s*=\s*("[^"]*"|[\w.:/@-]*|\{.*|\[.*)\s*,?   |   # hcl attribute, .env assignment
        [\w./-]+:\s*                                       |   # yaml key opening a block
        -?\s*[\w"./${}-]+:\s+("[^"]*"|[\w.:/@${}-]+)          |   # yaml key: value
        -\s+"?\d[\d.:]*(/\w+)?"?                           |   # yaml port mapping
        [{}\[\]],?                                         |   # lone bracket
        \}\s*,?
    )\s*$""",
    re.VERBOSE,
)


class Repo:
    def __init__(self, root: Path, sha: str) -> None:
        self.root = root
        self.sha = sha
        self._lines: dict[str, list[str]] = {}

    def files(self, pattern: str) -> list[str]:
        out = []
        for p in sorted(self.root.glob(pattern)):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and "/.terraform/" not in f"/{rel}" and not rel.startswith(".git/"):
                out.append(rel)
        return out

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def text(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            self._lines[rel] = self.text(rel).splitlines()
        return self._lines[rel]

    def source(self, rel: str, start: int, end: int | None = None, *, inner: bool = True, leading: bool = True) -> Source:
        """A Source for lines start..end (1-based, inclusive) with their comments."""
        end = end or start
        lines = self.lines(rel)
        comments: list[str] = []
        if leading:
            comments += leading_comments(lines, start)
        if inner:
            comments += [""] + _comment_text(lines[start - 1 : end])
        return Source(rel, start, end, _paragraphs(comments))

    def header(self, rel: str) -> list[str]:
        """The comment block a file opens with, before its first code line."""
        block: list[str] = []
        for line in self.lines(rel):
            if not line.strip():
                if block:
                    block.append("")
                continue
            m = _COMMENT.match(line)
            if not m or line.lstrip().startswith("#!"):
                if not m:
                    break
                continue
            block.append(m.group(2))
        return _paragraphs(_clean(block))


def leading_comments(lines: list[str], start: int) -> list[str]:
    """The comment block directly above line start. A section banner (a block
    with a `# ----` rule in it) one blank line further up belongs to the first
    item under it, so it is included too."""
    block, i = _block_above(lines, start - 2)
    if i >= 1 and not lines[i].strip():
        banner, _ = _block_above(lines, i - 1)
        if any(_RULE.match(c) and c.strip() for c in banner):
            block = banner + [""] + block
    return _clean(block)


def _block_above(lines: list[str], i: int) -> tuple[list[str], int]:
    block: list[str] = []
    while i >= 0:
        m = _COMMENT.match(lines[i])
        if not m:
            break
        block.insert(0, m.group(2))
        i -= 1
    return block, i


def _comment_text(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        m = _COMMENT.match(line)
        if m:
            out.append(m.group(2))
        else:
            # A trailing comment after code: `key: value  # why`.
            t = re.search(r"\s#\s(.+)$", line)
            if t and "'" not in line.split("#")[0][-2:] and '"' not in line[t.start():]:
                out.append(t.group(1))
            elif out and out[-1] != "":
                out.append("")
    return _clean(out)


def _clean(block: list[str]) -> list[str]:
    out = []
    for c in block:
        if _RULE.match(c) and c.strip():
            continue
        if c.strip() and _CODE.match(c):
            continue
        out.append(c.rstrip())
    return out


def _paragraphs(lines: list[str]) -> list[str]:
    """Join comment lines into paragraphs, split on blank lines."""
    paras: list[str] = []
    cur: list[str] = []
    for line in lines + [""]:
        if line.strip():
            # Keep indented lines (examples, commands) on their own line.
            if line.startswith("  ") and cur:
                cur.append("\n" + line)
            else:
                cur.append(line.strip())
        elif cur:
            paras.append(" ".join(cur).replace(" \n", "\n"))
            cur = []
    return paras
