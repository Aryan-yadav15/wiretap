"""Where work starts outside a request, and shared thread pools: run_in_threadpool / to_thread / run_in_executor,
asyncio.create_task, scheduler.add_job / @scheduled_job, BackgroundTasks.add_task, @app.on_event."""

import ast

from .pyast import dotted, text

NAME, KIND, LABEL = "jobs", "jobs", "Background jobs and thread pools"
THREADPOOL_CALLS = {"run_in_threadpool": "threadpool", "to_thread": "default-threads", "run_in_executor": "default-threads"}
SCHEDULER_CALLS = {"add_job", "scheduled_job"}


def call(x, qual, node, visit):
    func = node.func
    last = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
    if last in THREADPOOL_CALLS:
        x.add("resources", {"fn": qual, "res": THREADPOOL_CALLS[last], "line": node.lineno}, NAME)
        args = node.args[1:] if last == "run_in_executor" else node.args
        if args:
            target = args[0]
            if isinstance(target, ast.Lambda):
                visit(target.body)
            else:
                td = dotted(target)
                if td:
                    x.calls.append({"from": qual, "target": td, "line": node.lineno, "via": last})
    if last == "create_task" and node.args and isinstance(node.args[0], ast.Call):
        td = dotted(node.args[0].func)
        if td:
            x.add("spawns", {"from": qual, "target": td, "kind": "background task", "line": node.lineno}, NAME)
    if last in SCHEDULER_CALLS and node.args:
        td = dotted(node.args[0])
        if td:
            x.add("spawns", {"from": qual, "target": td, "kind": "scheduled job", "line": node.lineno}, NAME)
    if last == "add_task" and node.args:
        td = dotted(node.args[0])
        if td:
            x.add("spawns", {"from": qual, "target": td, "kind": "background task", "line": node.lineno}, NAME)


def decorator(x, qual, d, fn):
    if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
        return
    if d.func.attr == "on_event" and d.args:
        x.add("spawns", {"from": None, "target": qual, "kind": f"app {(text(d.args[0]) or 'event')}", "line": d.lineno}, NAME)
    elif d.func.attr in SCHEDULER_CALLS:
        x.add("spawns", {"from": None, "target": qual, "kind": "scheduled job", "line": d.lineno}, NAME)
