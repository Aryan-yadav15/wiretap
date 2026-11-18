# Codebase

```
wiretap/                    the server (Python standard library only)
  __main__.py               CLI: serve, index, install-hooks, uninstall-hooks
  graph.py                  Project (files, cache, scan) and the linker that builds the graph
  py_extract.py             Python parser (ast)
  ts_extract.mjs            TypeScript parser (TypeScript compiler API), run as a Node child process
  style_extract.py          stylesheet rules as "functions" (CSS, SCSS, Sass, Less); HTML files listed
  session.py                hook events → sessions, tasks, timeline, function diffs, shell-read parsing
  server.py                 HTTP + SSE server, projects (State, ProjectState), file watcher, hook queue, search, summary
web/                        the UI: index.html, style.css, app.js (no build step)
claude-mod/wiretap-live/    Claude Code mod: the button above the prompt and /wiretap
tests/                      unittest suite and a small fixture project (FastAPI backend + TS web app)
docs/                       these docs, diagrams and README cards
wiretap.config.example.json starting point for wiretap.config.json (optional; git ignores it)
package.json                pins TypeScript; nothing else uses Node packages
```

## Entry points

| What | File |
|---|---|
| CLI | [`wiretap/__main__.py`](../wiretap/__main__.py) (`main`) |
| Server start | [`wiretap/server.py`](../wiretap/server.py) (`serve`, at the bottom) |
| Hook payloads | `POST /api/hook` in [`wiretap/server.py`](../wiretap/server.py#L456), then [`State.on_hook`](../wiretap/server.py#L293) picks the project, then [`Sessions.on_hook`](../wiretap/session.py#L303) |
| UI start | the `start()` function at the bottom of [`web/app.js`](../web/app.js) |
| Terminal button | [`claude-mod/wiretap-live/hooks/register.tsx`](../claude-mod/wiretap-live/hooks/register.tsx) |

## Where to start

| To change… | Look at |
|---|---|
| what is extracted from Python (a new framework, a new DB client) | [`py_extract.py`](../wiretap/py_extract.py), then how [`graph.py`](../wiretap/graph.py) `link_py` turns it into nodes and edges |
| what is extracted from TypeScript (a new HTTP client shape, a new event prop) | [`ts_extract.mjs`](../wiretap/ts_extract.mjs), then `link_ts` in [`graph.py`](../wiretap/graph.py) |
| how API calls match routes | `match_route` in [`graph.py`](../wiretap/graph.py#L787) |
| how a folder without a config is mapped (which apps, size limit) | `autodetect` in [`graph.py`](../wiretap/graph.py#L59), called from `State.for_cwd` in [`server.py`](../wiretap/server.py#L270) |
| what counts as a rule in a stylesheet | `extract` in [`style_extract.py`](../wiretap/style_extract.py) |
| which outside services are recognised | `DEFAULT_HOSTS`, `DEFAULT_EXTERNALS_PY`, `DEFAULT_EXTERNALS_TS` in [`graph.py`](../wiretap/graph.py), or `hosts` / `externalsPython` / `externalsJs` in the config |
| which file goes in which lane (screen, component, style, route, logic, data) | `role` in [`graph.py`](../wiretap/graph.py#L347), or `roles` in the config |
| what a Claude tool call records (new tool, new shell command) | `on_hook` and `shell_files` in [`session.py`](../wiretap/session.py) |
| a view in the UI | `viewSystem`, `viewMap`, `viewFile`, `viewLive`, `viewTrace`, `viewResources` in [`web/app.js`](../web/app.js) |
| the Live question view (rows per flow, replay) | `lookedFrom`, `buildStrands`, `drawStrands` in [`web/app.js`](../web/app.js) |
| how Live words a step and groups steps into phases | `describeStep`, `phasesOf`, `STEP_HINT` in [`web/app.js`](../web/app.js) |
| the canvas: pan, zoom, dragging boxes and columns, box-select | `mountSvg`, `fitView`, `zoomAt`, `moveBoxes`, `moveLane`, `pickInBox` in [`web/app.js`](../web/app.js) |
| the sidebar, resizable and folding panels | `renderSide`, `setUI`, `applyUI`, `foldable` in [`web/app.js`](../web/app.js) |
| what gets installed into Claude Code | `hook_command`, `hooks_block`, `install` in [`__main__.py`](../wiretap/__main__.py) |

## Conventions

- **No dependencies.** The server uses only the Python standard library; the only Node package is TypeScript.
- **IDs are strings with a kind prefix:** `f:` file, `fn:` function, `rt:` route, `tb:` table, `ext:` outside service, `res:` shared resource, `tg:` where a flow starts. Edge kinds are `call`, `http`, `renders`, `read`, `write`, `db`, `ext`, `res`, `trig`.
- **The UI is one file per concern** (HTML, CSS, JS) with hash routing (`#v=map&id=…&p=<project>`), so any view can be linked. Colours are CSS variables on `:root`, with a dark set.
- **Tests** ([`tests/test_wiretap.py`](../tests/test_wiretap.py)) index the fixture project, check what was linked, and replay a Claude session through the hook handler and HTTP API. Run them with `python3 -m unittest discover -s tests -v`.
