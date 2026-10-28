"""A small evaluator for the HCL expressions Terraform stacks use in locals and
for_each: for-expressions, conditionals, string templates, attribute and index
access, and the handful of functions that build maps and names.

Anything it cannot know (a data source, a resource attribute, an unsupported
function) evaluates to UNKNOWN. Inside a string template an unknown part is
kept as the WILD character, so "jserrats-tfstate-${data...account_id}" still
matches "jserrats-tfstate-590183967526" (see matches()).
"""

from __future__ import annotations

import re

import hcl2
from lark import Token, Tree

WILD = "￿"


class _Unknown:
    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


UNKNOWN = _Unknown()


def matches(pattern: str, value: str) -> bool:
    """Does a partly-known string match a literal one?"""
    if not isinstance(pattern, str) or not isinstance(value, str):
        return False
    if WILD not in pattern:
        return pattern == value
    rx = ".+".join(re.escape(p) for p in pattern.split(WILD))
    return re.fullmatch(rx, value) is not None


def display(v) -> str:
    if isinstance(v, str):
        return v.replace(WILD, "…")
    return str(v)


class Block:
    def __init__(self, type_: str, labels: list[str], attrs: dict, blocks: list["Block"]) -> None:
        self.type = type_
        self.labels = labels
        self.attrs = attrs
        self.blocks = blocks


def parse(text: str) -> list[Block | tuple[str, Tree]]:
    """Top-level items of a file, in order: Blocks, and (name, expr) attributes."""
    tree = hcl2.parses_to_tree(text)
    body = tree.children[0]
    return _body_items(body)


def _body_items(body: Tree) -> list:
    out = []
    for c in body.children:
        if not isinstance(c, Tree):
            continue
        if c.data == "attribute":
            out.append((_ident(c.children[0]), _expr_child(c)))
        elif c.data == "block":
            type_ = _ident(c.children[0])
            labels = []
            inner = None
            for x in c.children[1:]:
                if isinstance(x, Tree) and x.data == "identifier":
                    labels.append(_ident(x))
                elif isinstance(x, Tree) and x.data == "string":
                    labels.append(_string_literal(x))
                elif isinstance(x, Tree) and x.data == "body":
                    inner = x
            items = _body_items(inner) if inner is not None else []
            attrs = {k: v for k, v in (i for i in items if isinstance(i, tuple))}
            blocks = [i for i in items if isinstance(i, Block)]
            out.append(Block(type_, labels, attrs, blocks))
    return out


def _ident(t: Tree) -> str:
    return str(t.children[0])


def _expr_child(attr: Tree) -> Tree:
    for x in attr.children[1:]:
        if isinstance(x, Tree):
            return x
    raise ValueError("attribute without expression")


def _string_literal(t: Tree) -> str:
    return "".join(str(p.children[0]) for p in t.children if isinstance(p, Tree) and isinstance(p.children[0], Token))


def _trees(t: Tree) -> list[Tree]:
    return [c for c in t.children if isinstance(c, Tree) and c.data != "new_line_or_comment"]


class Evaluator:
    def eval(self, t, env: dict):
        if isinstance(t, Token):
            return UNKNOWN
        fn = getattr(self, "_" + t.data, None)
        if fn is None:
            return UNKNOWN
        try:
            return fn(t, env)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            return UNKNOWN

    # --- terms -----------------------------------------------------------
    def _expr_term(self, t, env):
        kids = _trees(t)
        return self.eval(kids[0], env) if kids else UNKNOWN

    def _literal_value(self, t, env):
        return {"TRUE": True, "FALSE": False, "NULL": None}[t.children[0].type]

    def _int_lit(self, t, env):
        return int(str(t.children[0]))

    def _float_lit(self, t, env):
        return float(str(t.children[0]))

    def _identifier(self, t, env):
        name = _ident(t)
        return env.get(name, UNKNOWN)

    def _string(self, t, env):
        out = []
        for p in t.children:
            if not isinstance(p, Tree):
                continue
            inner = p.children[0]
            if isinstance(inner, Token):
                out.append(str(inner))
            else:  # interpolation
                v = self.eval(_trees(inner)[0], env)
                out.append(_to_str(v))
        return "".join(out)

    def _heredoc_template(self, t, env):
        return UNKNOWN

    _heredoc_template_trim = _heredoc_template

    def _tuple(self, t, env):
        return [self.eval(c, env) for c in _trees(t)]

    def _object(self, t, env):
        out = {}
        for elem in _trees(t):
            key_t, val_t = _trees(elem)
            key_inner = _trees(key_t)[0]
            if key_inner.data == "identifier":
                key = _ident(key_inner)
            elif key_inner.data == "expr_term" and _trees(key_inner) and _trees(key_inner)[0].data == "identifier" and len(key_inner.children) == 1:
                key = _ident(_trees(key_inner)[0])
            else:
                key = self.eval(key_inner, env)
            if key is UNKNOWN or not isinstance(key, str):
                continue
            out[key] = self.eval(val_t, env)
        return out

    def _get_attr_expr_term(self, t, env):
        base_t, attr_t = _trees(t)
        base = self.eval(base_t, env)
        name = _ident(_trees(attr_t)[0])
        if isinstance(base, dict):
            # A missing attribute is an omitted optional(): null.
            return base.get(name)
        return UNKNOWN

    def _index_expr_term(self, t, env):
        base_t, idx_t = _trees(t)
        base = self.eval(base_t, env)
        idx = self.eval(_trees(idx_t)[0], env)
        if base is UNKNOWN or idx is UNKNOWN:
            return UNKNOWN
        if isinstance(base, dict):
            return base.get(idx, UNKNOWN)
        if isinstance(base, list) and isinstance(idx, int):
            return base[idx]
        return UNKNOWN

    def _attr_splat_expr_term(self, t, env):
        base_t, splat_t = _trees(t)
        base = self.eval(base_t, env)
        name = _ident(_trees(_trees(splat_t)[0])[0])
        if isinstance(base, list):
            return [x.get(name) if isinstance(x, dict) else UNKNOWN for x in base]
        return UNKNOWN

    _full_splat_expr_term = _attr_splat_expr_term

    # --- operators -------------------------------------------------------
    def _unary_op(self, t, env):
        op = t.children[0].type
        v = self.eval(_trees(t)[0], env)
        if v is UNKNOWN:
            return UNKNOWN
        return -v if op == "MINUS" else not v

    def _binary_op(self, t, env):
        left_t, term = _trees(t)
        op_t, right_t = _trees(term)
        op = str(op_t.children[0])
        a = self.eval(left_t, env)
        if op == "&&" and a is False:
            return False
        if op == "||" and a is True:
            return True
        b = self.eval(right_t, env)
        if a is UNKNOWN or b is UNKNOWN:
            return UNKNOWN
        return {
            "==": lambda: a == b,
            "!=": lambda: a != b,
            "&&": lambda: a and b,
            "||": lambda: a or b,
            "+": lambda: a + b,
            "-": lambda: a - b,
            "*": lambda: a * b,
            "/": lambda: a / b,
            "%": lambda: a % b,
            "<": lambda: a < b,
            ">": lambda: a > b,
            "<=": lambda: a <= b,
            ">=": lambda: a >= b,
        }[op]()

    def _conditional(self, t, env):
        cond_t, yes_t, no_t = _trees(t)
        c = self.eval(cond_t, env)
        if c is UNKNOWN:
            return UNKNOWN
        return self.eval(yes_t if c else no_t, env)

    # --- for-expressions -------------------------------------------------
    def _for_iter(self, intro: Tree, env):
        names = [_ident(x) for x in _trees(intro) if x.data == "identifier"]
        coll = self.eval(_trees(intro)[-1], env)
        if isinstance(coll, dict):
            items = list(coll.items())
        elif isinstance(coll, list):
            items = list(enumerate(coll))
        else:
            return None
        envs = []
        for k, v in items:
            e = dict(env)
            if len(names) == 2:
                e[names[0]], e[names[1]] = k, v
            else:
                e[names[0]] = v
            envs.append(e)
        return envs

    def _for_tuple_expr(self, t, env):
        kids = _trees(t)
        envs = self._for_iter(kids[0], env)
        if envs is None:
            return UNKNOWN
        cond = next((k for k in kids if k.data == "for_cond"), None)
        out = []
        for e in envs:
            if cond is not None and self.eval(_trees(cond)[0], e) is not True:
                continue
            out.append(self.eval(kids[1], e))
        return out

    def _for_object_expr(self, t, env):
        kids = _trees(t)
        envs = self._for_iter(kids[0], env)
        if envs is None:
            return UNKNOWN
        cond = next((k for k in kids if k.data == "for_cond"), None)
        out = {}
        for e in envs:
            if cond is not None and self.eval(_trees(cond)[0], e) is not True:
                continue
            k = self.eval(kids[1], e)
            if isinstance(k, str):
                out[k] = self.eval(kids[2], e)
        return out

    # --- functions -------------------------------------------------------
    def _function_call(self, t, env):
        name = _ident(_trees(t)[0])
        args_t = next((x for x in _trees(t) if x.data == "arguments"), None)
        args = []
        expand = False
        if args_t is not None:
            for c in args_t.children:
                if isinstance(c, Token) and c.type == "ELLIPSIS":
                    expand = True
                elif isinstance(c, Tree) and c.data != "new_line_or_comment":
                    args.append(c)
        if name == "try":
            for a in args:
                v = self.eval(a, env)
                if v is not UNKNOWN:
                    return v
            return UNKNOWN
        vals = [self.eval(a, env) for a in args]
        if expand and vals and isinstance(vals[-1], list):
            vals = vals[:-1] + vals[-1]
        f = _FUNCS.get(name)
        if f is None or any(v is UNKNOWN for v in vals):
            return UNKNOWN
        return f(*vals)


def _to_str(v) -> str:
    if v is UNKNOWN or isinstance(v, (dict, list)):
        return WILD
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _merge(*maps):
    out = {}
    for m in maps:
        if isinstance(m, dict):
            out.update(m)
    return out


def _flatten(xs):
    out = []
    for x in xs:
        out.extend(_flatten(x) if isinstance(x, list) else [x])
    return out


def _distinct(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


_FUNCS = {
    "merge": _merge,
    "concat": lambda *ls: [x for l in ls for x in l],
    "flatten": _flatten,
    "distinct": _distinct,
    "toset": _distinct,
    "tolist": list,
    "keys": lambda m: sorted(m.keys()),
    "values": lambda m: [m[k] for k in sorted(m)],
    "lookup": lambda m, k, d=None: m.get(k, d),
    "contains": lambda l, x: x in l,
    "length": len,
    "join": lambda sep, l: sep.join(_to_str(x) for x in l),
    "format": lambda f, *a: f.replace("%%", "\0") % tuple(a) if "%s" in f else UNKNOWN,
    "lower": lambda s: s.lower(),
    "upper": lambda s: s.upper(),
    "coalesce": lambda *a: next((x for x in a if x not in (None, "")), None),
    "tostring": _to_str,
    "startswith": lambda s, p: s.startswith(p),
    "endswith": lambda s, p: s.endswith(p),
    "basename": lambda s: s.rsplit("/", 1)[-1],
    "compact": lambda l: [x for x in l if x],
    "setsubtract": lambda a, b: [x for x in a if x not in b],
}


def apply_type(type_t, value):
    """Fill in optional(type, default) attributes the way Terraform does when
    it converts a value to a variable's type."""
    if type_t is None or value is UNKNOWN or value is None:
        return value
    call = _unwrap_call(type_t)
    if call is None:
        return value
    name, args = call
    if name in ("map",) and isinstance(value, dict):
        return {k: apply_type(args[0], v) for k, v in value.items()}
    if name in ("list", "set") and isinstance(value, list):
        return [apply_type(args[0], v) for v in value]
    if name == "object" and isinstance(value, dict):
        out = dict(value)
        for key, attr_t in _object_type_attrs(args[0]):
            inner = _unwrap_call(attr_t)
            if inner and inner[0] == "optional":
                if out.get(key) is None and len(inner[1]) > 1:
                    out[key] = Evaluator().eval(inner[1][1], {})
                if key in out:
                    out[key] = apply_type(inner[1][0], out[key])
            elif key in out:
                out[key] = apply_type(attr_t, out[key])
        return out
    return value


def _unwrap_call(t):
    while isinstance(t, Tree) and t.data == "expr_term" and len(_trees(t)) == 1:
        t = _trees(t)[0]
    if isinstance(t, Tree) and t.data == "function_call":
        name = _ident(_trees(t)[0])
        args_t = next((x for x in _trees(t) if x.data == "arguments"), None)
        return name, (_trees(args_t) if args_t is not None else [])
    return None


def _object_type_attrs(t):
    while isinstance(t, Tree) and t.data == "expr_term" and len(_trees(t)) == 1:
        t = _trees(t)[0]
    if not (isinstance(t, Tree) and t.data == "object"):
        return []
    out = []
    for elem in _trees(t):
        key_t, val_t = _trees(elem)
        k = _trees(key_t)[0]
        while k.data == "expr_term":
            k = _trees(k)[0]
        if k.data == "identifier":
            out.append((_ident(k), val_t))
        elif k.data == "string":
            out.append((_string_literal(k), val_t))
    return out
