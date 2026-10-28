"""Line positions in HCL, which python-hcl2 does not report.

python-hcl2 gives the values; this scanner gives where they are. It masks
strings, heredocs and comments, tracks brace depth per line, and from that
finds top-level blocks and attributes, and the entries one level inside them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import hcl2

_BLOCK = re.compile(r'^(\w+)((?:\s+(?:"[^"]*"|[\w-]+))*)\s*\{')
_ATTR = re.compile(r'^\s*("?[\w./@-]+"?)\s*[=:]')
_REF = re.compile(
    r"\b(module\.[\w-]+|var\.[\w-]+|local\.[\w-]+|data\.[a-z0-9_]+\.[\w-]+|[a-z][a-z0-9]*_[a-z0-9_]+\.[\w-]+)"
)


@dataclass
class Span:
    kind: str          # "block" or "attr" or "entry"
    name: str          # block type, attribute or entry key
    labels: list[str]
    start: int         # 1-based
    end: int
    depth: int


class Scan:
    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.masked: list[str] = []
        self.depth_at: list[int] = []
        self._scan()

    def _scan(self) -> None:
        depth = 0
        heredoc: str | None = None
        in_block_comment = False
        for line in self.raw:
            self.depth_at.append(depth)
            if heredoc:
                if line.strip() == heredoc:
                    heredoc = None
                self.masked.append("")
                continue
            out = []
            i = 0
            in_str = False
            interp = 0
            while i < len(line):
                c = line[i]
                if in_block_comment:
                    if line.startswith("*/", i):
                        in_block_comment = False
                        i += 2
                        continue
                    i += 1
                    continue
                if in_str:
                    # Keep what is inside ${...}: that is where references are.
                    if c == "\\":
                        i += 2
                        continue
                    if line.startswith("${", i):
                        interp += 1
                        out.append(" ")
                        i += 2
                        continue
                    if c == "}" and interp:
                        interp -= 1
                        out.append(" ")
                    elif c == '"' and not interp:
                        in_str = False
                        out.append('"')
                    elif interp:
                        out.append(c)
                    i += 1
                    continue
                if c == '"':
                    in_str = True
                    out.append('"')
                elif c == "#" or line.startswith("//", i):
                    break
                elif line.startswith("/*", i):
                    in_block_comment = True
                    i += 2
                    continue
                elif line.startswith("<<", i):
                    m = re.match(r"<<-?(\w+)", line[i:])
                    if m:
                        heredoc = m.group(1)
                        out.append('""')
                        break
                    out.append(c)
                else:
                    if c in "{[(":
                        depth += 1
                    elif c in "}])":
                        depth -= 1
                    out.append(c)
                i += 1
            self.masked.append("".join(out))
        self.depth_at.append(depth)

    def _end(self, start: int) -> int:
        """Last line (1-based) of the construct opening on line start."""
        base = self.depth_at[start - 1]
        for i in range(start, len(self.raw) + 1):
            if self.depth_at[i] <= base and (i == start or self.masked[i - 1].strip()):
                return i
        return len(self.raw)

    def top(self) -> list[Span]:
        spans = []
        for i, line in enumerate(self.masked, start=1):
            if self.depth_at[i - 1] != 0 or not line.strip():
                continue
            m = _BLOCK.match(line)
            if m:
                labels = _labels(self.raw[i - 1])
                spans.append(Span("block", m.group(1), labels, i, self._end(i), 0))
                continue
            m = _ATTR.match(line)
            if m:
                spans.append(Span("attr", m.group(1).strip('"'), [], i, self._end(i), 0))
        return spans

    def inside(self, span: Span) -> list[Span]:
        """Entries one level inside a span: map keys, attributes, nested blocks
        and object elements of a list (named by their index)."""
        want = span.depth + 1
        out = []
        index = 0
        for i in range(span.start + 1, span.end + 1):
            line = self.masked[i - 1]
            if self.depth_at[i - 1] != want or not line.strip():
                continue
            m = _BLOCK.match(line.strip())
            if m and not _ATTR.match(line):
                out.append(Span("block", m.group(1), _labels(self.raw[i - 1]), i, self._end(i), want))
                continue
            m = _ATTR.match(line)
            if m:
                out.append(Span("entry", m.group(1).strip('"'), [], i, self._end(i), want))
                continue
            if line.strip().startswith("{"):
                out.append(Span("entry", str(index), [], i, self._end(i), want))
                index += 1
        return out

    def body(self, span: Span) -> str:
        return "\n".join(self.masked[span.start - 1 : span.end])

    def raw_body(self, span: Span) -> str:
        return "\n".join(self.raw[span.start - 1 : span.end])


def _labels(raw: str) -> list[str]:
    m = _BLOCK.match(raw.strip())
    return [a or b for a, b in re.findall(r'"([^"]*)"|([\w-]+)', m.group(2))] if m else []


def refs(text: str) -> set[str]:
    """Every reference in an expression: module.x, var.x, type.name, ..."""
    found = set()
    for m in _REF.finditer(text):
        r = m.group(1)
        if r.split(".")[0] in ("each", "count", "path", "terraform", "self"):
            continue
        found.add(r)
    return found


def values(text: str) -> dict:
    """The file's values, with string quotes stripped and comments dropped."""
    opts = hcl2.SerializationOptions(with_comments=False, strip_string_quotes=True)
    return hcl2.loads(text, serialization_options=opts)


def literal(v):
    """A python-hcl2 value as a plain literal, or None if it is an expression."""
    if isinstance(v, str):
        if v.startswith("${") and v.endswith("}"):
            return None
        return v
    return v
