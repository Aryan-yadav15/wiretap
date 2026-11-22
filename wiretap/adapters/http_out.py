"""Outbound HTTP to a literal host from Python: httpx.post("https://api.example.com/..."), requests.get(...)."""

import ast

from .pyast import text

NAME, KIND, LABEL = "http_out", "http", "Outbound HTTP (Python)"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}


def call(x, qual, node, visit):
    func = node.func
    last = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    if last not in METHODS or not node.args:
        return
    arg = node.args[1] if last == "request" and len(node.args) > 1 else node.args[0]
    url = text(arg, x.consts)
    # a URL, not any string starting with "http": x.get("http_out", []) is a dict lookup
    if url and url.startswith(("http://", "https://")):
        host = url.split("/")[2]
        if host and "{}" not in host:
            x.add("http_out", {"fn": qual, "host": host, "url": url, "line": node.lineno}, NAME)
