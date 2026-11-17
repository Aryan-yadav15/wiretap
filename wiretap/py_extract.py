"""Extract functions, calls, routes, DB access and shared-resource use from one Python file.

The output is plain data (dicts/lists). Resolving names across files happens later, in graph.py.
"""

import ast
import hashlib

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
ROUTE_METHODS = HTTP_METHODS | {"websocket", "api_route"}
SUPABASE_WRITE = {"insert", "upsert", "update", "delete"}
SUPABASE_READ = {"select"}
THREADPOOL_CALLS = {"run_in_threadpool": "threadpool", "to_thread": "default-threads", "run_in_executor": "default-threads"}
SCHEDULER_CALLS = {"add_job", "scheduled_job"}


def _dotted(node):
    """`a.b.c` -> 'a.b.c'; anything else -> None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _str(node, consts=None, fvalues=None):
    """Best-effort string value of a node; f-string parts we can't resolve become '{}'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                d = _dotted(v.value)
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
        a, b = _str(node.left, consts, fvalues), _str(node.right, consts, fvalues)
        if a is not None and b is not None:
            return a + b
    return None


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
        x, qual = self.x, self.qual
        func = node.func
        d = _dotted(func)
        last = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)

        if d:
            x.calls.append({"from": qual, "target": d, "line": node.lineno})

        # Shared thread pools: run_in_threadpool(fn, ...), asyncio.to_thread(fn, ...), loop.run_in_executor(None, fn)
        if last in THREADPOOL_CALLS:
            x.resources.append({"fn": qual, "res": THREADPOOL_CALLS[last], "line": node.lineno})
            args = node.args[1:] if last == "run_in_executor" else node.args
            if args:
                target = args[0]
                if isinstance(target, ast.Lambda):
                    self.visit(target.body)
                else:
                    td = _dotted(target)
                    if td:
                        x.calls.append({"from": qual, "target": td, "line": node.lineno, "via": last})

        # Background work started from code
        if last == "create_task" and node.args and isinstance(node.args[0], ast.Call):
            td = _dotted(node.args[0].func)
            if td:
                x.spawns.append({"from": qual, "target": td, "kind": "background task", "line": node.lineno})
        if last in SCHEDULER_CALLS and node.args:
            td = _dotted(node.args[0])
            if td:
                x.spawns.append({"from": qual, "target": td, "kind": "scheduled job", "line": node.lineno})
        if last == "add_task" and node.args:
            td = _dotted(node.args[0])
            if td:
                x.spawns.append({"from": qual, "target": td, "kind": "background task", "line": node.lineno})

        # Supabase-style table access: Db.table("orders").select(...)
        if isinstance(func, ast.Attribute):
            if func.attr in SUPABASE_WRITE | SUPABASE_READ:
                tbl = self._find_table(func.value)
                if tbl:
                    op = "write" if func.attr in SUPABASE_WRITE else "read"
                    x.tables.append({"fn": qual, "table": tbl, "op": op, "line": node.lineno})
            elif func.attr == "table" and node.args:
                t = _str(node.args[0], x.consts)
                if t:
                    x.tables.append({"fn": qual, "table": t, "op": "access", "line": node.lineno})
            elif func.attr == "rpc" and node.args:
                t = _str(node.args[0], x.consts)
                if t:
                    x.tables.append({"fn": qual, "table": "rpc:" + t, "op": "call", "line": node.lineno})

        # Outbound HTTP to a literal host: httpx.post("https://api.example.com/...")
        if last in HTTP_METHODS | {"request"} and node.args:
            arg = node.args[1] if last == "request" and len(node.args) > 1 else node.args[0]
            url = _str(arg, x.consts)
            if url and url.startswith("http"):
                host = url.split("/")[2] if url.count("/") >= 2 else url
                if host and "{}" not in host:
                    x.http_out.append({"fn": qual, "host": host, "url": url, "line": node.lineno})

        self.generic_visit(node)

    def _find_table(self, node):
        while isinstance(node, (ast.Call, ast.Attribute)):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "table" and node.args:
                    return _str(node.args[0], self.x.consts)
                node = f
            else:
                node = node.value
        return None


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
        self.error = None

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
        # Router = APIRouter(prefix="/cart")   App = FastAPI(...)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            var, val = node.targets[0].id, node.value
            if isinstance(val, ast.Call):
                name = _dotted(val.func) or ""
                if name.endswith("APIRouter"):
                    prefix = ""
                    for kw in val.keywords:
                        if kw.arg == "prefix":
                            prefix = _str(kw.value, self.consts, self.fvalues) or ""
                    self.routers[var] = prefix
                elif name.endswith("FastAPI"):
                    self.app_vars.append(var)
        # X.include_router(child.Router, prefix="/auth")
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "include_router" and call.args:
                parent = _dotted(call.func.value)
                child = _dotted(call.args[0])
                prefix = ""
                for kw in call.keywords:
                    if kw.arg == "prefix":
                        prefix = _str(kw.value, self.consts, self.fvalues) or ""
                if parent and child:
                    self.includes.append({"parent": parent, "child": child, "prefix": prefix})
        # SQLAlchemy models: class Order(Base): __tablename__ = "orders"
        if isinstance(node, ast.ClassDef):
            self.classes.append(node.name)
            for b in node.body:
                if isinstance(b, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in b.targets):
                    t = _str(b.value)
                    if t:
                        self.model_tables[node.name] = t

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
                    self._route_decorator(d, qual, node)
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

    def _route_decorator(self, d, qual, fn_node):
        if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
            return
        method = d.func.attr
        owner = _dotted(d.func.value)
        if method in ROUTE_METHODS:
            path = _str(d.args[0], self.consts, self.fvalues) if d.args else None
            if path is None:
                for kw in d.keywords:
                    if kw.arg == "path":
                        path = _str(kw.value, self.consts, self.fvalues)
            if path is None:
                path = ""
            methods = [method.upper()]
            if method == "api_route":
                for kw in d.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [(_str(e) or "GET").upper() for e in kw.value.elts]
            for m in methods:
                self.routes.append({
                    "fn": qual, "owner": owner, "method": "WS" if m == "WEBSOCKET" else m, "path": path,
                    "line": d.lineno, "sync": isinstance(fn_node, ast.FunctionDef),
                })
        elif method == "on_event" and d.args:
            self.spawns.append({"from": None, "target": qual, "kind": f"app {(_str(d.args[0]) or 'event')}", "line": d.lineno})
        elif method in SCHEDULER_CALLS:
            self.spawns.append({"from": None, "target": qual, "kind": "scheduled job", "line": d.lineno})

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
