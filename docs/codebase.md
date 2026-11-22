# Codebase

```
wiretap/                    the server (Python standard library only)
  __main__.py               CLI: serve, index, install-hooks, uninstall-hooks
  graph.py                  Project (files, cache, scan) and the linker that builds the graph
  py_extract.py             Python parser (ast): functions, calls, imports; runs the Python adapters
  ts_extract.mjs            TypeScript parser (TypeScript compiler API), run as a Node child process; runs the TS adapters
  adapters/                 one file per framework or library: routes, tables, HTTP calls, jobs (see adapters.md)
  coverage.py               what was understood per app and what wasn't (other languages, libraries no adapter reads)
  style_extract.py          stylesheet rules as "functions" (CSS, SCSS, Sass, Less); HTML files listed
  session.py                hook events → sessions, tasks, timeline, function diffs, shell-read parsing
  server.py                 HTTP + SSE server, projects (State, ProjectState), file watcher, hook queue, search, summary
  simplify.py               Simple mode: plain-English names per file and flow step from claude -p, cached by function hash
web/                        the UI: index.html, style.css, app.js (no build step); vendor/elk: the layout library, copied in
claude-mod/wiretap-live/    Claude Code mod: the button above the prompt and /wiretap
tests/                      unittest suite; fixture (FastAPI + TS web app), fixture-stacks (Express, Next.js, Flask, Prisma)
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
| support a new framework, ORM or HTTP client | a new adapter in [`wiretap/adapters/`](../wiretap/adapters/): [adapters.md](adapters.md) has the record formats and examples |
| what the core reads from any Python / TypeScript file (functions, calls, imports, JSX) | [`py_extract.py`](../wiretap/py_extract.py) / [`ts_extract.mjs`](../wiretap/ts_extract.mjs), then `link_py` / `link_ts` in [`graph.py`](../wiretap/graph.py) |
| how routes get their prefixes (routers mounted in routers) | the `walk` over `includes` in `link_py` and `link_ts` in [`graph.py`](../wiretap/graph.py) |
| the coverage report (CLI, server start, System view) | [`coverage.py`](../wiretap/coverage.py), `diagnostics` in [`web/app.js`](../web/app.js) |
| how API calls match routes | `match_route` in [`graph.py`](../wiretap/graph.py#L787) |
| how a folder without a config is mapped (which apps, size limit) | `autodetect` in [`graph.py`](../wiretap/graph.py#L59), called from `State.for_cwd` in [`server.py`](../wiretap/server.py#L270) |
| what counts as a rule in a stylesheet | `extract` in [`style_extract.py`](../wiretap/style_extract.py) |
| which outside services are recognised | `DEFAULT_HOSTS`, `DEFAULT_EXTERNALS_PY`, `DEFAULT_EXTERNALS_TS` in [`graph.py`](../wiretap/graph.py), or `hosts` / `externalsPython` / `externalsJs` in the config |
| which file goes in which lane (screen, component, style, route, logic, data) | `role` in [`graph.py`](../wiretap/graph.py#L347), or `roles` in the config |
| what a Claude tool call records (new tool, new shell command) | `on_hook` and `shell_files` in [`session.py`](../wiretap/session.py) |
| Simple mode: what the model is asked, how answers are checked and cached, how flows are cut into steps | [`simplify.py`](../wiretap/simplify.py) ([ADR 0006](decisions/0006-simplify-layer.md)); in the UI `descOf`, `describeOnScreen`, `flowSection` in [`web/app.js`](../web/app.js) |
| a view in the UI | `viewSystem`, `viewMap`, `viewFile`, `viewLive`, `viewTrace`, `viewResources` in [`web/app.js`](../web/app.js) |
| the Live question view (rows per flow, replay) | `lookedFrom`, `buildStrands`, `drawStrands` in [`web/app.js`](../web/app.js) |
| how Live words a step and groups steps into phases | `describeStep`, `phasesOf`, `STEP_HINT` in [`web/app.js`](../web/app.js) |
| the canvas: pan, zoom, dragging boxes and columns, box-select | `mountSvg`, `fitView`, `zoomAt`, `moveBoxes`, `moveLane`, `pickInBox` in [`web/app.js`](../web/app.js) |
| how Map and Folders are laid out and drawn (ELK, line colours, zoom levels, hover) | `placeFlow`, `drawFlow`, `mountFlow`, `kindCls` in [`web/app.js`](../web/app.js) ([ADR 0007](decisions/0007-layered-layout.md)) |
| the Folders view: what's inside a folder and what's outside it | `viewTree`, `groupIn`, `deepen`, `dirIndex` in [`web/app.js`](../web/app.js) |
| the sidebar, resizable and folding panels | `renderSide`, `setUI`, `applyUI`, `foldable` in [`web/app.js`](../web/app.js) |
| what gets installed into Claude Code | `hook_command`, `hooks_block`, `install` in [`__main__.py`](../wiretap/__main__.py) |

## Conventions

- **No dependencies.** The server uses only the Python standard library; the only Node package is TypeScript. The UI has one copied-in library, ELK, for layout ([ADR 0007](decisions/0007-layered-layout.md)).
- **IDs are strings with a kind prefix:** `f:` file, `fn:` function, `rt:` route, `tb:` table, `ext:` outside service, `res:` shared resource, `tg:` where a flow starts. Edge kinds are `call`, `http`, `renders`, `read`, `write`, `db`, `ext`, `res`, `trig`.
- **The UI is one file per concern** (HTML, CSS, JS) with hash routing (`#v=map&id=…&p=<project>`), so any view can be linked. Colours are CSS variables on `:root`, with a dark set.
- **Tests** ([`tests/test_wiretap.py`](../tests/test_wiretap.py)) index the fixture project, check what was linked, and replay a Claude session through the hook handler and HTTP API. Run them with `python3 -m unittest discover -s tests -v`.
