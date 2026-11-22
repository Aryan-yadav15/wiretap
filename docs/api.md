# API

Wiretap has a CLI and a small HTTP API on `http://127.0.0.1:7878` (localhost only). The browser UI is the main client of the API; it's documented here for scripts and for contributors. Examples below are real responses from a run against a six-app project, trimmed with `…`.

## CLI

All commands take `--config <file>` (default `wiretap.config.json`, or `$WIRETAP_CONFIG`). Only `index` needs one, unless it is given `--root`; `serve` without one maps each folder Claude Code works in. Defined in [`wiretap/__main__.py`](../wiretap/__main__.py).

| Command | Options | Does |
|---|---|---|
| `python3 -m wiretap serve` | `--port N`, `--open` | index the config's project (if any), watch for changes, serve the UI and API, and map new folders as Claude works in them |
| `python3 -m wiretap index` | `--explain FILE`, `--root FOLDER` | index once and print what was understood per app (routes, tables, HTTP calls and jobs by adapter, HTTP calls matched to a route), code in other languages and libraries no adapter reads, unmatched API calls, calls that couldn't be followed, parse errors. `--explain` shows one file instead: its functions, every adapter record, and the routes and links made from them. `--root` maps a folder without a config, the way the server maps the folders Claude works in. |
| `python3 -m wiretap describe` | `--root FOLDER`, `--yes` | write the plain-English names Simple mode shows, ahead of time, for every file whose functions changed since they were last described: one `claude -p --model haiku` call per file. Asks first unless `--yes`. With `--root`, fills the same cache the server uses for that folder. ([ADR 0006](decisions/0006-simplify-layer.md)) |
| `python3 -m wiretap install-hooks` | `--settings <file>`, `--port N`, `--dry-run` | add the hooks and the terminal button to Claude Code's settings (backup first) |
| `python3 -m wiretap uninstall-hooks` | `--settings <file>` | remove exactly what `install-hooks` added |

## Projects: the `p` parameter

The server can hold several projects: the config's, plus one per folder Claude Code has worked in. `/api/graph`, `/api/session`, `/api/source` and `/api/search` take `?p=<project id>`; without it they use the config's project, or the one Claude used last when there is no config.

## `GET /api/projects`

Every mapped project. `primary` is the config's project, or `null`. Defined in [`server.py:384`](../wiretap/server.py#L384).

```json
{ "projects": [ { "id": "khaoogully", "name": "Khaoogully", "root": "/Users/…/khaoogully", "auto": false, "note": null,
                  "files": 642, "sessions": 0, "last": 0 },
                { "id": "coffee-walk", "name": "coffee-walk", "root": "/Users/…/coffee-walk", "auto": true, "note": null,
                  "files": 31, "sessions": 1, "last": 1794945455.7 }, … ],
  "primary": "khaoogully" }
```

`auto` is true for a folder mapped on the fly; `note` says why such a folder has no map (a home folder, no code, too many files).

## `GET /api/graph[?p=<project id>]`

The whole graph of one project, or `{"empty": true}` before any project is mapped. Defined in [`server.py:388`](../wiretap/server.py#L388).

```json
{
  "name": "Khaoogully", "version": 1,
  "stats": { "files": 638, "functions": 3831, "edges": 8704, "http_matched": 467, "unresolved_calls": 252, … },
  "apps": [ … ], "lanes": [ { "id": "screen", "label": "Screens" }, … ],
  "files": [ { "id": "f:customer-web:src/App.tsx", "app": "customer-web", "rel": "src/App.tsx", "label": "App.tsx", "role": "ui", "lines": 257, "error": null }, … ],
  "fns":   [ { "id": "fn:customer-web:src/App.tsx#AppContent", "file": "f:customer-web:src/App.tsx", "name": "AppContent", "line": 76, "end": 238, "hash": "d97ba046639e", "kind": "component" }, … ],
  "nodes": [ { "id": "rt:customer-api:GET /", "kind": "route", "method": "GET", "path": "/", "fn": "fn:customer-api:app/main.py#HealthCheck", … }, … ],
  "edges": [ { "s": "fn:customer-api:app/database.py#GetSupabaseClientWithAuth", "t": "fn:customer-api:app/database.py#_UserClient.__init__", "k": "call", "line": 160 }, … ],
  "unmatched_http": [], "unresolved": { … }, "timings": { … }, "generated": 1794927000.0, "toolDir": "…",
  "imports": { "f:customer-web:src/App.tsx": ["f:customer-web:src/services/api.ts", …], … },
  "coverage": { "apps": [ { "id": "customer-api", "files": 118, "functions": 1180, "found": { "routes": { "fastapi": 212 }, "tables": { "supabase": 640 } },
                            "http": { "calls": 0, "matched": 0, "unmatched": 0, "unknown_url": 0 }, "calls_not_followed": 40, … }, … ],
                "not_parsed": [ { "lang": "Go", "files": 14 } ], "not_read": [ { "app": "web", "dep": "drizzle-orm", "what": "Drizzle queries" } ] },
  "root": "/Users/…/khaoogully", "project": "khaoogully", "auto": false, "note": null
}
```

In a stylesheet each rule is listed in `fns` as a function named by its selector (`.card:hover`, `@media (max-width: 600px) › .nav`), so reads and edits are reported per rule. Stylesheets sit in the `style` lane.

## `GET /api/source?file=<file id>&start=<line>&end=<line>`

Lines of a project file, at most 400 per request; add `&p=` for a project other than the default. `404` when the file isn't in the index. Defined in [`server.py:408`](../wiretap/server.py#L408).

```json
{ "file": "f:customer-api:app/routers/admin.py", "start": 1,
  "lines": ["\"\"\"", "Admin API routes for managing platform resources.", "\"\"\""] }
```

## `GET /api/search?q=<words, comma separated>`

The Trace view: functions whose code mentions the words as identifiers (`cod` matches `isCod` and `cod_fee`, not `code`), plus feature-specific tables they use and functions that use those tables or call the hits. Takes `&p=`. Defined in [`server.py:418`](../wiretap/server.py#L418).

```json
{ "terms": ["delivery_partner"],
  "hits": [ { "id": "fn:customer-web:src/pages/admin/OrderDetailPage.tsx#OrderDetailPage", "count": 12, "terms": ["delivery_partner"],
              "lines": [[498, "partnerName={order.delivery_partner_name}"], …] }, … ],
  "tables": ["tb:admin-pg:restaurant_commission_tiers", …],
  "by_data": [ … ], "by_call": [ … ],
  "file_hits": { "f:customer-web:src/pages/admin/OrderDetailPage.tsx": 12, … } }
```

## `GET /api/session[?id=<session id>][&p=<project id>]`

A Claude Code session and the list of all sessions in every project, newest first. With `id`, that session, whichever project it's in; without it, the latest session in the project `p`. Defined in [`server.py:394`](../wiretap/server.py#L394).

```json
{ "session": { "id": "…", "source": "claude", "cwd": "…", "started": 1794927880.1, "last": 1794927912.4,
    "tasks": [ { "prompt": "Do we have a proper architecture for setting up a new delivery provider? …",
                 "events": [ { "i": 1, "t": 1794927886.4, "k": "read", "file": "f:customer-api:app/services/delivery_partners/__init__.py", "via": "shell" }, … ],
                 "touched": {}, "reads": [ … ], "looked": [ { "file": "…", "fns": [ { "name": "ParsePayload", "how": "read", "score": 1.0 } ] }, … ],
                 "planned": [], "ripple_files": [], "tests": null,
                 "summary": { "edited": 0, "created": 0, "deleted": 0, "fns": {}, "ripple_files": 0, "reads": 10, "off_plan": [], … } } ] },
  "sessions": [ { "id": "…", "source": "claude", "last": 1794927912.4, "project": "khaoogully", "projectName": "Khaoogully", "prompt": "…" } ] }
```

Event kinds (`k`): `prompt`, `read`, `search`, `run` (other shell command), `plan`, `edit`, `create`, `delete`, `test`, `stop`.

## `GET /api/stream`

Server-sent events for every project. Each message is JSON:

- `{"type": "graph", "project": "<id>", "version": N}`: that project's graph changed (fetch `/api/graph?p=<id>` again).
- `{"type": "session", "project": "<id>", "session": {…}}`: the same session object as above.
- `{"type": "projects"}`: a new folder was mapped (fetch `/api/projects` again).
- `{"type": "describe", "project": "<id>", "file": "<file id>", "fns": {…}, "files": {…}}`: one file was described for Simple mode (same shape as `GET /api/describe`), or `{…, "error": "…"}` when that failed.

Defined in [`server.py:422`](../wiretap/server.py#L422).

## `POST /api/hook`

Receives a Claude Code hook payload (the JSON Claude Code writes to the hook's stdin). Answers immediately and processes it in the background. The payload's `cwd` decides the project: the mapped project that contains it, or a new one mapped from that folder ([`State.on_hook`](../wiretap/server.py#L293)). A session stays in the project its first hook went to. Defined in [`server.py:456`](../wiretap/server.py#L456).

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"hook_event_name":"Stop","session_id":"doc-example"}' http://127.0.0.1:7878/api/hook
```

```json
{"ok": true}
```

| Status | When |
|---|---|
| 400 | the body isn't JSON |

## `POST /api/summarize`

Body `{"session": "<id>", "task": <index>}`. Runs `claude -p --model haiku` on the task's git diff and returns `{"text": "…"}`. When the task changed nothing it returns a fixed text without calling Claude. Errors come back as `{"error": "…"}` (unknown session, no `claude` on PATH, timeout after 3 minutes). Besides Simple mode (below), this is the only call that uses tokens. Defined in [`server.py:194`](../wiretap/server.py#L194).

## Simple mode: `GET /api/describe`, `POST /api/describe`, `POST /api/flow`

Plain-English names over the parsed map ([ADR 0006](decisions/0006-simplify-layer.md)). The parser owns the structure; a model only names files, functions and flow steps. Descriptions are cached in `describe.json` beside the extraction cache, each function's against its hash, so changed code is described again the next time it is shown. Defined in [`wiretap/simplify.py`](../wiretap/simplify.py) and `ProjectState.describe` / `ProjectState.flow` in [`server.py`](../wiretap/server.py).

`GET /api/describe[?p=<project id>]` returns what is already described and still matches the code. No model call.

```json
{"fns": {"fn:api:app/routers/orders.py#create_order": {"n": "Places a new order", "t": "Prices the items, applies the payment method, and stores the order total."}},
 "files": {"f:api:app/routers/orders.py": {"n": "Order endpoints", "t": "Lets customers place a new order and look up an existing one by ID."}}}
```

`POST /api/describe` with `{"p": "<project id>", "files": ["<file id>", …]}` queues up to 24 files that need a description, in the order given (the UI puts the file in focus first), and answers `{"queued": [<file ids>]}` at once. Files already described are left out. Each result arrives as a `describe` event on `/api/stream`. One `claude -p --model haiku` call per file (files with more than 20 functions are split into calls run side by side), asking only about functions whose code changed; names for functions the parser didn't find are dropped. Up to 8 calls run at once; set `WIRETAP_DESCRIBE_WORKERS` to change that.

`POST /api/flow` with `{"p": "<project id>", "ids": [<trigger, route, function and table ids, in flow order>]}` cuts the flow into steps where the layer changes (screen, UI, client, route, logic, data), describes the flow's files if needed, then makes one call to name the steps. It answers when done:

```json
{"title": "Placing an order prices items and stores the total",
 "steps": [{"name": "Place a new order", "layer": "route", "ids": ["rt:api:POST /api/orders", "fn:api:app/routers/orders.py#create_order"]}]}
```

A response that doesn't name every step is rejected. Errors come back as `{"error": "…"}` (no `claude` on PATH, timeout, an answer that isn't JSON).
