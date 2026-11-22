"""Small helpers for reading Python syntax trees, shared by the extractor and the Python adapters."""

import ast


def dotted(node):
    """`a.b.c` -> 'a.b.c'; anything else -> None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def text(node, consts=None, fvalues=None):
    """Best-effort string value of a node; f-string parts we can't resolve become '{}'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                d = dotted(v.value)
                if fvalues and d in fvalues:
                    out.append(fvalues[d])
                elif consts and d in consts:
                    out.append(consts[d])
                else:
                    out.append("{}")
        return "".join(out)
    if isinstance(node, ast.Name) and consts and node.id in consts:
        return consts[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = text(node.left, consts, fvalues), text(node.right, consts, fvalues)
        if a is not None and b is not None:
            return a + b
    return None


def keyword(call, name):
    """The value node of keyword argument `name` in a call, or None."""
    return next((kw.value for kw in call.keywords if kw.arg == name), None)
