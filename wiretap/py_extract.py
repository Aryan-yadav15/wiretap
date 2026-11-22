"""Extract functions, calls, routes, DB access and shared-resource use from one Python file.

The output is plain data (dicts/lists). Resolving names across files happens later, in graph.py.
"""

import ast
import hashlib

from .adapters import PY_ADAPTERS
from .adapters.pyast import dotted as _dotted, text as _str

HOOKS = {h: [getattr(a, h) for a in PY_ADAPTERS if hasattr(a, h)] for h in ("module", "decorator", "call")}


def _hash(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


class _FnCollector(ast.NodeVisitor):
    """Walks one function body, without descending into nested defs (they are separate units)."""

    def __init__(self, x, qual):
        self.x, self.qual = x, qual

    def visit_FunctionDef(self, node):
        pass  # nested defs are collected separately

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        pass

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id[:1].isupper():
            self.x.name_refs.append({"fn": self.qual, "name": node.id})

    def visit_Call(self, node):
        d = _dotted(node.func)
        if d:
            self.x.calls.append({"from": self.qual, "target": d, "line": node.lineno})
        for hook in HOOKS["call"]:
            hook(self.x, self.qual, node, self.visit)
        self.generic_visit(node)


class PyExtract:
    def __init__(self, src, rel, module, fvalues=None):
        self.src, self.rel, self.module = src, rel, module
        self.fvalues = fvalues or {}
        self.lines = src.splitlines()
        self.imports = {}       # local name -> dotted target ("app.services.pricing" or "app.services.pricing.fn")
        self.ext_modules = {}   # local name -> top-level third-party module
        self.consts = {}
        self.functions, self.calls, self.routes, self.tables = [], [], [], []
        self.resources, self.spawns, self.http_out, self.name_refs = [], [], [], []
        self.routers, self.includes, self.app_vars, self.model_tables = {}, [], [], {}
        self.classes = []
        self.flask_vars = set()  # Flask() / Blueprint() objects in this file (see adapters/flask.py)
        self.error = None

    def add(self, kind, record, via):
        record["via"] = via
        getattr(self, kind).append(record)

    def _resolve_relative(self, level, mod):
        if not level:
            return mod or ""
        base = self.module.split(".")
        is_pkg = self.rel.endswith("__init__.py")
        base = base if is_pkg else base[:-1]
        base = base[: len(base) - (level - 1)] if level > 1 else base
        return ".".join([p for p in base + ([mod] if mod else []) if p])

    def run(self):
        try:
            tree = ast.parse(self.src)
        except SyntaxError as e:
            self.error = f"syntax error line {e.lineno}"
            return self

        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                s = _str(node.value, self.consts)
                if s is not None:
                    self.consts[node.targets[0].id] = s

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    self.imports[local] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                base = self._resolve_relative(node.level, node.module)
                for a in node.names:
                    if a.name == "*":
                        continue
                    self.imports[a.asname or a.name] = f"{base}.{a.name}" if base else a.name

        for node in tree.body:
            self._module_level(node)

        self._collect_defs(tree.body, prefix="", cls=None)
        return self

    def _module_level(self, node):
        if isinstance(node, ast.ClassDef):
            self.classes.append(node.name)
        for hook in HOOKS["module"]:
            hook(self, node)

    def _text(self, node):
        start = node.lineno - 1
        if node.decorator_list:
            start = min(d.lineno for d in node.decorator_list) - 1
        return "\n".join(self.lines[start: node.end_lineno])

    def _collect_defs(self, body, prefix, cls):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + node.name
                decos = [_dotted(d.func if isinstance(d, ast.Call) else d) or "" for d in node.decorator_list]
                self.functions.append({
                    "name": qual, "line": node.lineno, "end": node.end_lineno,
                    "async": isinstance(node, ast.AsyncFunctionDef), "cls": cls,
                    "hash": _hash(self._text(node)), "decorators": decos,
                })
                for d in node.decorator_list:
                    for hook in HOOKS["decorator"]:
                        hook(self, qual, d, node)
                c = _FnCollector(self, qual)
                for stmt in node.body:
                    c.visit(stmt)
                for a in node.args.defaults + node.args.kw_defaults:
                    if a is not None:
                        c.visit(a)  # Depends(get_db) etc.
                # nested functions become their own units: outer.inner
                self._collect_defs(list(self._child_defs(node)), qual + ".", cls)
            elif isinstance(node, ast.ClassDef) and not prefix:
                self._collect_defs(node.body, node.name + ".", node.name)

    @staticmethod
    def _child_defs(node):
        """Function defs directly inside `node`'s body (not inside a deeper def)."""
        stack = list(ast.iter_child_nodes(node))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield n
            elif not isinstance(n, (ast.ClassDef, ast.Lambda)):
                stack.extend(ast.iter_child_nodes(n))

    def as_dict(self):
        return {
            "lang": "py", "rel": self.rel, "module": self.module, "error": self.error,
            "imports": self.imports, "functions": self.functions, "calls": self.calls,
            "routes": self.routes, "routers": self.routers, "includes": self.includes, "app_vars": self.app_vars,
            "tables": self.tables, "model_tables": self.model_tables, "classes": self.classes, "name_refs": self.name_refs,
            "resources": self.resources, "spawns": self.spawns, "http_out": self.http_out,
            "lines": len(self.lines),
        }


def extract(src, rel, module, fvalues=None):
    return PyExtract(src, rel, module, fvalues).run().as_dict()
