# API

Wiretap has a CLI and a small HTTP API on `http://127.0.0.1:7878` (localhost only). The browser UI is the main client of the API; it's documented here for scripts and for contributors. Examples below are real responses from a run against a six-app project, trimmed with `…`.

## CLI

All commands take `--config <file>` (default `wiretap.config.json`, or `$WIRETAP_CONFIG`). Defined in [`wiretap/__main__.py`](../wiretap/__main__.py).

| Command | Options | Does |
|---|---|---|
| `python3 -m wiretap serve` | `--port N`, `--open` | index, watch for changes, serve the UI and API |
| `python3 -m wiretap index` | | index once and print a report: files per app, API calls matched / unmatched, calls that couldn't be followed, parse errors |
| `python3 -m wiretap install-hooks` | `--settings <file>`, `--port N`, `--dry-run` | add the hooks and the terminal button to Claude Code's settings (backup first) |
| `python3 -m wiretap uninstall-hooks` | `--settings <file>` | remove exactly what `install-hooks` added |

## `GET /api/graph`

The whole graph. Defined in [`server.py:293`](../wiretap/server.py#L293).

```json
{
  "name": "Khaoogully", "version": 1,
  "stats": { "files": 638, "functions": 3831, "edges": 8704, "http_matched": 467, "unresolved_calls": 252, … },
  "apps": [ … ], "lanes": [ { "id": "screen", "label": "Screens" }, … ],
  "files": [ { "id": "f:customer-web:src/App.tsx", "app": "customer-web", "rel": "src/App.tsx", "label": "App.tsx", "role": "ui", "lines": 257, "error": null }, … ],
  "fns":   [ { "id": "fn:customer-web:src/App.tsx#AppContent", "file": "f:customer-web:src/App.tsx", "name": "AppContent", "line": 76, "end": 238, "hash": "d97ba046639e", "kind": "component" }, … ],
  "nodes": [ { "id": "rt:customer-api:GET /", "kind": "route", "method": "GET", "path": "/", "fn": "fn:customer-api:app/main.py#HealthCheck", … }, … ],
  "edges": [ { "s": "fn:customer-api:app/database.py#GetSupabaseClientWithAuth", "t": "fn:customer-api:app/database.py#_UserClient.__init__", "k": "call", "line": 160 }, … ],
  "unmatched_http": [], "unresolved": { … }, "timings": { … }, "generated": 1794927000.0, "toolDir": "…"
}
```

## `GET /api/source?file=<file id>&start=<line>&end=<line>`

Lines of a project file, at most 400 per request. `404` when the file isn't in the index. Defined in [`server.py:308`](../wiretap/server.py#L308).

```json
{ "file": "f:customer-api:app/routers/admin.py", "start": 1,
  "lines": ["\"\"\"", "Admin API routes for managing platform resources.", "\"\"\""] }
```

## `GET /api/search?q=<words, comma separated>`

The Trace view: functions whose code mentions the words as identifiers (`cod` matches `isCod` and `cod_fee`, not `code`), plus feature-specific tables they use and functions that use those tables or call the hits. Defined in [`server.py:318`](../wiretap/server.py#L318).

```json
{ "terms": ["delivery_partner"],
  "hits": [ { "id": "fn:customer-web:src/pages/admin/OrderDetailPage.tsx#OrderDetailPage", "count": 12, "terms": ["delivery_partner"],
              "lines": [[498, "partnerName={order.delivery_partner_name}"], …] }, … ],
  "tables": ["tb:admin-pg:restaurant_commission_tiers", …],
  "by_data": [ … ], "by_call": [ … ],
  "file_hits": { "f:customer-web:src/pages/admin/OrderDetailPage.tsx": 12, … } }
```

## `GET /api/session[?id=<session id>]`

A Claude Code session (the latest one without `id`) and the list of all sessions. Defined in [`server.py:296`](../wiretap/server.py#L296).

```json
{ "session": { "id": "…", "source": "claude", "cwd": "…", "started": 1794927880.1, "last": 1794927912.4,
    "tasks": [ { "prompt": "Do we have a proper architecture for setting up a new delivery provider? …",
                 "events": [ { "i": 1, "t": 1794927886.4, "k": "read", "file": "f:customer-api:app/services/delivery_partners/__init__.py", "via": "shell" }, … ],
                 "touched": {}, "reads": [ … ], "looked": [ { "file": "…", "fns": [ { "name": "ParsePayload", "how": "read", "score": 1.0 } ] }, … ],
                 "planned": [], "ripple_files": [], "tests": null,
                 "summary": { "edited": 0, "created": 0, "deleted": 0, "fns": {}, "ripple_files": 0, "reads": 10, "off_plan": [], … } } ] },
  "sessions": [ { "id": "…", "source": "claude", "last": 1794927912.4, "prompt": "…" } ] }
```

Event kinds (`k`): `prompt`, `read`, `search`, `run` (other shell command), `plan`, `edit`, `create`, `delete`, `test`, `stop`.

## `GET /api/stream`

Server-sent events. Each message is JSON: `{"type": "graph", "version": N}` when the graph changed (fetch `/api/graph` again), or `{"type": "session", "session": {…}}` with the same session object as above. Defined in [`server.py:322`](../wiretap/server.py#L322).

## `POST /api/hook`

Receives a Claude Code hook payload (the JSON Claude Code writes to the hook's stdin). Answers immediately and processes it in the background. Defined in [`server.py:356`](../wiretap/server.py#L356).

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

Body `{"session": "<id>", "task": <index>}`. Runs `claude -p --model haiku` on the task's git diff and returns `{"text": "…"}`. When the task changed nothing it returns a fixed text without calling Claude. Errors come back as `{"error": "…"}` (unknown session, no `claude` on PATH, timeout after 3 minutes). This is the only call that uses tokens. Defined in [`server.py:197`](../wiretap/server.py#L197).
