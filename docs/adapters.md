# Adapters: teaching Wiretap a framework

Wiretap's core parsers find functions, calls and imports in any Python or TypeScript/JavaScript file. Everything that depends on a framework or library, such as how a route is declared, how a table is read, or what an HTTP call looks like, lives in an **adapter**: one small file that spots that pattern and adds a plain record for it. The linker in [`graph.py`](../wiretap/graph.py) turns those records into the map, whichever adapter made them. A new framework is one new file plus a fixture, and you don't need to touch the linker.

## What's there

| Adapter | Language | Adds | Recognises |
|---|---|---|---|
| [`fastapi`](../wiretap/adapters/fastapi.py) | Python | routes | `@router.get("/x")`, `APIRouter(prefix=…)`, `include_router(…, prefix=…)` |
| [`flask`](../wiretap/adapters/flask.py) | Python | routes | `@app.route("/x", methods=[…])`, `@bp.get("/x")`, `Blueprint(url_prefix=…)`, `register_blueprint(…, url_prefix=…)` |
| [`supabase`](../wiretap/adapters/supabase.py) | Python | tables | `Db.table("orders").select()` / `.insert()`, `Db.rpc("fn")` |
| [`sqlalchemy`](../wiretap/adapters/sqlalchemy.py) | Python | tables | `class Order(Base): __tablename__ = "orders"` (a function naming `Order` uses `orders`) |
| [`jobs`](../wiretap/adapters/jobs.py) | Python | jobs | `run_in_threadpool`, `asyncio.create_task`, `scheduler.add_job`, `BackgroundTasks.add_task`, `@app.on_event` |
| [`http_out`](../wiretap/adapters/http_out.py) | Python | HTTP | `httpx.post("https://…")`, `requests.get(…)` to a literal host |
| [`ts_http`](../wiretap/adapters/ts_http.mjs) | TS / JS | HTTP | `fetch(url, {method})`, `api.get(url)` / `axios.post(url)` (client names from `httpClients`) |
| [`ts_supabase`](../wiretap/adapters/ts_supabase.mjs) | TS / JS | tables | `supabase.from('orders').select()`, `supabase.rpc('fn')`, `supabase.storage.from('b')` |
| [`ts_express`](../wiretap/adapters/ts_express.mjs) | TS / JS | routes | `app.get('/x/:id', handler)` on Express, Fastify or Hono; `app.use('/api', router)`, `app.route('/api', sub)` |
| [`ts_nextjs`](../wiretap/adapters/ts_nextjs.mjs) | TS / JS | routes | `app/api/**/route.ts` (GET, POST… exports), `pages/api/**`, Expo Router `+api.ts` |
| [`ts_prisma`](../wiretap/adapters/ts_prisma.mjs) | TS / JS | tables | `prisma.orderItem.findMany()`, table names from `schema.prisma` (`@@map`) |

Libraries a project depends on that no adapter reads yet are listed in `NOT_READ` in [`adapters/__init__.py`](../wiretap/adapters/__init__.py), so the coverage report can name them instead of leaving a silent gap. When you add an adapter, take its library off that list.

## See what Wiretap found

```bash
python3 -m wiretap index                          # per app: routes, tables, HTTP calls, jobs, by adapter; what wasn't read
python3 -m wiretap index --explain path/to/file   # one file: functions, every adapter record, and the routes and links made from them
```

The same report is printed when the server maps a project, and appears as **What Wiretap understood** in the System view.

## Records

Adapters add records to lists on the file's extraction. Every record gets `via` (the adapter's name), which the coverage report counts. `fn` / `from` is the name of the function the record sits in: `name`, `Class.method` or `outer.inner`.

| List | Record | Notes |
|---|---|---|
| `routes` | `{fn, owner, method, path, line}` | `method` is `GET`… or `ANY`; path params as `{id}`. `owner` is the router variable, so mounts can add prefixes. In TypeScript, `target` (a dotted name) can replace `fn` when the handler is defined elsewhere; the linker resolves it through imports. `fn: null` keeps the route without a handler. |
| `routers` | `{variable: prefix}` | a router object and its own prefix (`APIRouter(prefix=…)`, `Blueprint(url_prefix=…)`, `express.Router()`) |
| `app_vars` | `[variable]` | the root app objects routers are mounted under; when there are none, mounting starts from routers nothing else includes |
| `includes` | `{parent, child, prefix}` | `parent` mounts `child` (a variable, imported or local) under `prefix`. TypeScript can give `spec` (a `require('./x')` path) instead of `child`. |
| `tables` | `{fn, table, op, line}` | `op`: `read`, `write`, `access` (unknown) or `call`. `table`: a name, `rpc:fn` for a database function, `storage:bucket` (Supabase), `prisma:accessor` (mapped to the table by the linker). |
| `http` (TS) | `{from, method, url, line}` | `url` is a list of parts: `{s: "literal"}`, `{p: 1}` for a value, `{ref: "API.ORDERS"}` for a constant the linker looks up |
| `http_out` (Python) | `{fn, host, url, line}` | a call to an outside service |
| `spawns` | `{from, target, kind, line}` | where work starts outside a request: `kind` is `background task`, `scheduled job`, `app startup`… |
| `resources` | `{fn, res, line}` | shared pools (`threadpool`, `default-threads`) |

Python also has `model_tables` (`{Class: table}`); a function that mentions the class uses the table.

## A Python adapter

A module in [`wiretap/adapters/`](../wiretap/adapters/) with `NAME`, `KIND` (`routes`, `tables`, `http` or `jobs`), `LABEL` and any of three hooks. Helpers for reading the syntax tree are in [`pyast.py`](../wiretap/adapters/pyast.py): `dotted(node)` gives `"a.b.c"`, `text(node, x.consts)` the string value (f-string holes become `{}`), `keyword(call, "name")` a keyword argument.

```python
"""Litestar route handlers: @get("/orders/{order_id:int}"), @post("/orders")."""

import ast

from .pyast import text

NAME, KIND, LABEL = "litestar", "routes", "Litestar routes"
VERBS = {"get", "post", "put", "patch", "delete"}


def decorator(x, qual, d, fn):        # every decorator on a function or method
    if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id in VERBS and d.args:
        x.add("routes", {"fn": qual, "owner": None, "method": d.func.id.upper(),
                         "path": text(d.args[0], x.consts) or "", "line": d.lineno}, NAME)
```

The other two hooks are `module(x, node)` for every top-level statement and `call(x, qual, node, visit)` for every call inside a function (`visit(sub_node)` walks a sub-tree as part of it).

Then add it to `PY_ADAPTERS` in [`adapters/__init__.py`](../wiretap/adapters/__init__.py). Adapters run in that order, and each one sees every node: two adapters may both record the same thing, so check a shared marker first if they could overlap (see how `flask` and `fastapi` share `x.flask_vars`).

## A TypeScript adapter

A `ts_<name>.mjs` file in [`wiretap/adapters/`](../wiretap/adapters/). Any file with that name is loaded, so there's nothing to register. [`ts_http.mjs`](../wiretap/adapters/ts_http.mjs) documents the shape:

```js
export default {
  name: 'drizzle', kind: 'tables', label: 'Drizzle queries',
  statement(ctx, st) {},     // each top-level statement, after the core registered its functions
  call(ctx, n, c) {          // each call: c = {from, d, parts, method, obj, line}; return true to claim it
    if (!c.from || c.d !== 'db.select') return false;
    // ...find the table in .from(orders) further up the chain...
    ctx.add('tables', { fn: c.from, table: 'orders', op: 'read', line: c.line }, 'drizzle');
    return true;
  },
  file(ctx) {},              // once the whole file is read (file-based routing)
};
```

`ctx` holds the TypeScript module (`ctx.ts`), the source file (`ctx.sf`), `ctx.rel`, the extraction so far (`ctx.out`) and helpers: `dotted`, `urlParts`, `fnLike` (unwraps `useCallback(() => …)`), `register(node, name, parent)` to make an inline function a unit of its own (as Express does for inline route handlers), `ownerOf(node)`, and `ctx.state`, a scratch object for your adapter in this file. A claimed call isn't also recorded as a plain function call.

## Test it

1. Add the smallest project that uses the framework to [`tests/fixture-stacks/`](../tests/fixture-stacks/) (or a new fixture folder), and its app to that folder's `wiretap.config.json`.
2. Add a test to [`tests/test_wiretap.py`](../tests/test_wiretap.py) (`TestStacks` shows the pattern): build the project, then check the routes, tables or HTTP links your records turned into.
3. Run `python3 -m unittest discover -s tests -v`, and `python3 -m wiretap index` on a real project that uses the framework. The coverage lines should count your adapter, and `--explain` should show its records.

Bump `EXTRACT_FORMAT` in [`graph.py`](../wiretap/graph.py) when what the parsers or adapters record changes, so cached extractions are redone.

## What can't be followed

Static reading misses anything decided at runtime: routes built in loops, table names from variables, handlers looked up by string, ORM relationships loaded lazily. An adapter should record what it can read with confidence and leave the rest out. The coverage report and the System view's "Calls the map could not follow" make the gaps visible.
