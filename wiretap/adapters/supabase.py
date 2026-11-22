"""Supabase from Python: Db.table("orders").select(...) / .insert(...), Db.rpc("fn")."""

import ast

from .pyast import text

NAME, KIND, LABEL = "supabase", "tables", "Supabase tables"
WRITE = {"insert", "upsert", "update", "delete"}
READ = {"select"}


def _table_of(x, node):
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "table" and node.args:
                return text(node.args[0], x.consts)
            node = f
        else:
            node = node.value
    return None


def call(x, qual, node, visit):
    func = node.func
    if not isinstance(func, ast.Attribute):
        return
    if func.attr in WRITE | READ:
        t = _table_of(x, func.value)
        if t:
            x.add("tables", {"fn": qual, "table": t, "op": "write" if func.attr in WRITE else "read", "line": node.lineno}, NAME)
    elif func.attr == "table" and node.args:
        t = text(node.args[0], x.consts)
        if t:
            x.add("tables", {"fn": qual, "table": t, "op": "access", "line": node.lineno}, NAME)
    elif func.attr == "rpc" and node.args:
        t = text(node.args[0], x.consts)
        if t:
            x.add("tables", {"fn": qual, "table": "rpc:" + t, "op": "call", "line": node.lineno}, NAME)
