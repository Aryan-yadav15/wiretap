"""FastAPI routes: @router.get("/path"), APIRouter(prefix=...), app.include_router(child, prefix=...)."""

import ast

from .pyast import dotted, keyword, text

NAME, KIND, LABEL = "fastapi", "routes", "FastAPI routes"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
ROUTE_METHODS = HTTP_METHODS | {"websocket", "api_route"}


def module(x, node):
    # Router = APIRouter(prefix="/cart")   App = FastAPI(...)
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
        var, name = node.targets[0].id, dotted(node.value.func) or ""
        if name.endswith("APIRouter"):
            x.routers[var] = text(keyword(node.value, "prefix"), x.consts, x.fvalues) or ""
        elif name.endswith("FastAPI"):
            x.app_vars.append(var)
    # X.include_router(child.Router, prefix="/auth")
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "include_router" and call.args:
            parent, child = dotted(call.func.value), dotted(call.args[0])
            if parent and child:
                x.add("includes", {"parent": parent, "child": child, "prefix": text(keyword(call, "prefix"), x.consts, x.fvalues) or ""}, NAME)


def decorator(x, qual, d, fn):
    if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute) or d.func.attr not in ROUTE_METHODS:
        return
    if dotted(d.func.value) in x.flask_vars:
        return  # Flask 2's @bp.get(...): the flask adapter's
    method = d.func.attr
    path = text(d.args[0], x.consts, x.fvalues) if d.args else text(keyword(d, "path"), x.consts, x.fvalues)
    methods = [method.upper()]
    if method == "api_route":
        m = keyword(d, "methods")
        if isinstance(m, (ast.List, ast.Tuple)):
            methods = [(text(e) or "GET").upper() for e in m.elts]
    for m in methods:
        x.add("routes", {"fn": qual, "owner": dotted(d.func.value), "method": "WS" if m == "WEBSOCKET" else m, "path": path or "",
                         "line": d.lineno, "sync": isinstance(fn, ast.FunctionDef)}, NAME)
