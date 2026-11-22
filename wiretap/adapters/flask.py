"""Flask routes: @app.route("/path", methods=[...]) and Flask 2's @bp.get("/path"), Blueprint(url_prefix=...),
app.register_blueprint(bp, url_prefix=...). The Flask and Blueprint objects are noted in x.flask_vars, so the fastapi
adapter leaves their @x.get(...) decorators to this one."""

import ast
import re

from .pyast import dotted, keyword, text

NAME, KIND, LABEL = "flask", "routes", "Flask routes"
VERBS = {"get", "post", "put", "patch", "delete"}


def _path(p):
    # /orders/<int:order_id> -> /orders/{order_id}
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", p or "")


def module(x, node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
        var, name = node.targets[0].id, dotted(node.value.func) or ""
        if name.endswith("Blueprint"):
            x.routers[var] = _path(text(keyword(node.value, "url_prefix"), x.consts, x.fvalues))
            x.flask_vars.add(var)
        elif name == "Flask" or name.endswith(".Flask"):
            x.app_vars.append(var)
            x.flask_vars.add(var)
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call(x, None, node.value, None)


def call(x, qual, node, visit):
    # app.register_blueprint(orders.bp, url_prefix="/orders"), often inside create_app()
    if isinstance(node.func, ast.Attribute) and node.func.attr == "register_blueprint" and node.args:
        parent, child = dotted(node.func.value), dotted(node.args[0])
        if parent and child:
            x.add("includes", {"parent": parent, "child": child, "prefix": _path(text(keyword(node, "url_prefix"), x.consts, x.fvalues))}, NAME)


def decorator(x, qual, d, fn):
    if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute) or not d.args:
        return
    owner, attr = dotted(d.func.value), d.func.attr
    if attr == "route":
        methods = keyword(d, "methods")
        methods = [(text(e) or "GET").upper() for e in methods.elts] if isinstance(methods, (ast.List, ast.Tuple)) else ["GET"]
    elif attr in VERBS and owner in x.flask_vars:
        methods = [attr.upper()]
    else:
        return
    for m in methods:
        x.add("routes", {"fn": qual, "owner": owner, "method": m, "path": _path(text(d.args[0], x.consts, x.fvalues)), "line": d.lineno}, NAME)
