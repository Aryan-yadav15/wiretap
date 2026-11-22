# Architecture

Wiretap is one local Python process. It parses a project's Python and TypeScript files (and its stylesheets, rule by rule) into a graph (apps, files, functions, API routes, tables, outside services, and the links between them), keeps that graph current as files change, and serves it to a browser UI. Claude Code reports each tool call to the same process through hooks, so the UI can show what Claude reads and changes on top of the graph. One server holds several projects: the one in the config, if any, and each folder Claude Code works in, mapped on its first hook. Nothing runs inside your project and nothing leaves your machine.

## Components

| Component | Responsibility | Code |
|---|---|---|
| CLI | `serve`, `index`, `install-hooks`, `uninstall-hooks` | [`wiretap/__main__.py`](../wiretap/__main__.py) |
| Python parser | functions, calls, imports; runs the Python adapters | [`wiretap/py_extract.py`](../wiretap/py_extract.py) |
| TypeScript parser | functions, components, handlers, imports (ES and CommonJS), JSX `onClick` / `onPress`, renders; runs the TypeScript adapters | [`wiretap/ts_extract.mjs`](../wiretap/ts_extract.mjs) |
| Adapters | one per framework or library: routes, router prefixes and mounts, table use, HTTP calls, jobs | [`wiretap/adapters/`](../wiretap/adapters/), [adapters.md](adapters.md) |
| Coverage | per app, what each adapter found and how many API calls matched; other languages and unread libraries | [`wiretap/coverage.py`](../wiretap/coverage.py) |
| Stylesheet reader | each CSS / SCSS / Less rule as a "function" named by its selector, so edits are reported per rule; HTML pages with the scripts and stylesheets they load, and which stylesheets a script's class names come from | [`wiretap/style_extract.py`](../wiretap/style_extract.py) |
| Project + linker | which files to read, the extraction cache, and linking: imports, HTTP calls to routes, tables, resources, where flows start; `autodetect` for a folder with no config | [`wiretap/graph.py`](../wiretap/graph.py) |
| Sessions | turns hook events into a timeline: reads (including shell `grep` / `sed`), plan, function-level diffs, tests | [`wiretap/session.py`](../wiretap/session.py) |
| Server | HTTP + server-sent events, file watcher, hook queue routed to the right project, feature-trace search, optional summary. `State` holds every project; one `ProjectState` per project holds its graph and sessions | [`wiretap/server.py`](../wiretap/server.py) |
| UI | System, Folders, Map, Inside a file, Live, Trace, Resources views, a project sidebar, a canvas you can pan, zoom and rearrange; plain JS + SVG, no build step; Map and Folders laid out by ELK (copied into `web/vendor/elk`) | [`web/`](../web/) |
| Terminal button | a Claude Code mod: "◉ Wiretap live ↗" above the prompt and `/wiretap` | [`claude-mod/wiretap-live/`](../claude-mod/wiretap-live/) |

## How they communicate

- **Browser ↔ server:** plain HTTP for the graph, source excerpts, search and sessions; one long-lived server-sent-events stream (`/api/stream`) pushes graph, session and new-project updates to every open tab, each tagged with its project ([`server.py:422`](../wiretap/server.py#L422)).
- **Claude Code → server:** each hooked event runs one `curl` that POSTs the hook payload to `/api/hook` ([`__main__.py:23`](../wiretap/__main__.py#L23)). The server answers before doing any work and queues the payload ([`server.py:456`](../wiretap/server.py#L456)); one worker thread processes the queue in order ([`server.py:288`](../wiretap/server.py#L288)) and sends each payload to the project its `cwd` is in, mapping that folder first if no project covers it ([`server.py:270`](../wiretap/server.py#L270)).
- **Server → TypeScript parser:** a Node child process per batch of files, reading JSON on stdin and writing JSON on stdout ([`graph.py:289`](../wiretap/graph.py#L289)).
- **Server → Claude:** only when you press **Summarize with Claude**: `claude -p --model haiku` with the git diff on stdin ([`server.py:226`](../wiretap/server.py#L226)); in Simple mode ([`simplify.py`](../wiretap/simplify.py)); and in **Ask**: `claude -p --model sonnet --effort high` in the project folder with read-only tools, its answer streamed to the browser as it writes ([`ask.py`](../wiretap/ask.py)).

## Main flow: what happens on one Claude Code tool call

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/live-hook-dark.svg"/>
  <img src="diagrams/live-hook-light.svg" width="100%" alt="Sequence: Claude Code posts the hook to the HTTP server, which answers 200 at once and queues the payload. The hook worker passes it to Sessions, which re-parses the file through the code index for edits, gets the old and new functions back, and returns an event with a function diff. The worker pushes a session snapshot to the browser over SSE. On click only, the browser asks for a summary, the server runs claude -p --model haiku and returns the summary."/>
</picture>

<sub>[Interactive version](diagrams/live-hook.html), with source links. Download and open it in a browser.</sub>

1. Claude Code runs the hook: one `curl -m 1` POST that always exits 0 ([`__main__.py:23-26`](../wiretap/__main__.py#L23-L26)). The hooked events are SessionStart, UserPromptSubmit, PreToolUse (Bash), PostToolUse (Read, Edit, MultiEdit, Write, NotebookEdit, Bash, ExitPlanMode, Grep, Glob) and Stop ([`__main__.py:29-37`](../wiretap/__main__.py#L29-L37)).
2. The server answers `200` and puts the payload on a queue ([`server.py:456-460`](../wiretap/server.py#L456-L460)).
3. The worker picks the project by session id, or by `cwd` for a new session ([`server.py:293`](../wiretap/server.py#L293)), and that project calls `Sessions.on_hook` ([`server.py:92`](../wiretap/server.py#L92), [`session.py:303`](../wiretap/session.py#L303)). Reads and searches are recorded with the functions on the lines Claude saw ([`session.py:233`](../wiretap/session.py#L233)); edits re-parse that one file and diff it per function ([`session.py:136`](../wiretap/session.py#L136)).
4. The server pushes the session snapshot over SSE ([`server.py:104`](../wiretap/server.py#L104)); the Live view redraws, and follows Claude into that project if it's another one.

## Second flow: how a codebase becomes the map

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/indexing-dark.png"/>
  <img src="diagrams/indexing-light.png" width="100%" alt="Dataflow: Python files go through py_extract (Python ast) and TS/TSX files through ts_extract (TypeScript compiler API). Both write facts per file to extract.json in .wiretap-cache. The linker reads all files and builds the graph of imports, routes and tables, served at GET /api/graph and fetched by the browser map."/>
</picture>

<sub>[Interactive version](diagrams/indexing.html), with source links.</sub>

1. `Project.scan` lists each app's files and re-extracts only those whose modification time or size changed ([`graph.py:207`](../wiretap/graph.py#L207)). A watcher runs it for every project every second ([`server.py:312`](../wiretap/server.py#L312)).
2. Python files are parsed with `ast` ([`py_extract.py:321`](../wiretap/py_extract.py#L321)); TypeScript files with the TypeScript compiler API, without type checking ([`ts_extract.mjs:86`](../wiretap/ts_extract.mjs#L86)); stylesheets with a small rule scanner ([`style_extract.py:12`](../wiretap/style_extract.py#L12)).
3. Extractions are cached in `.wiretap-cache/extract.json` ([`graph.py:185`](../wiretap/graph.py#L185)), or `.wiretap-cache/auto/<project>/` for a folder mapped without a config.
4. The linker turns per-file facts into one graph: resolves imports and aliases, matches each API call to a backend route by method and path segments ([`graph.py:787`](../wiretap/graph.py#L787)), maps table reads and writes, and finds where each flow starts (a tap, a screen opening, a route, startup or a scheduled job).
5. The browser fetches `/api/graph` once and again whenever the server announces a new graph version.

## What the parsers understand

What follows is the core plus the adapters that ship; [adapters.md](adapters.md) lists them all.

- **Python / FastAPI / Flask**: functions, methods and nested functions; imports (including relative ones); `APIRouter(prefix=…)` and `include_router(…, prefix=…)` chains; route decorators; Flask `@app.route`, blueprints and `register_blueprint(…, url_prefix=…)`; Supabase `.table("x").select/insert/update/…` (read vs write) and `.rpc()`; SQLAlchemy models via `__tablename__`; `run_in_threadpool`, `asyncio.to_thread`, sync `def` endpoints (these use the thread pool too); `on_event("startup")`, `create_task`, `add_job`, `BackgroundTasks.add_task`; outside SDKs (Razorpay, Firebase, …) and HTTP calls to known hosts.
- **TypeScript / React / React Native**: functions, components, handlers nested in components, object-of-functions exports (`admin.getZones`), class instances (`orderService.getMyOrders`), re-exports, `@/` aliases; `api.get/post/…` and `fetch` calls with URLs from literals, template strings and constants like `API_ENDPOINTS.X(id)`; JSX `onClick` / `onPress` handlers (with the button text); `<Component/>` renders; direct `supabase.from("table")` calls; hooks like `const { addItem } = useCart()` (marked as a guess).
- **TypeScript / JavaScript backends**: Express, Fastify and Hono routes (`app.get('/x/:id', handler)`, routers mounted with `app.use('/api', router)`, handlers imported from other files, CommonJS `require` / `module.exports`); Next.js `app/**/route.ts` and `pages/api/**` routes and Expo Router `+api.ts`; Prisma queries, with table names from `schema.prisma`.
- **Stylesheets** (`.css`, `.scss`, `.sass`, `.less`) in a TypeScript app: every rule, named by its selector, with `@media` / `@supports` parents in the name and each `@keyframes` as one rule. They aren't linked to the components that use them.
- Each API call in an app is matched to its backend route (method + path, `{param}` aware). An app with no `backend` set is matched against every app that has routes, its own included (Next.js). With no `pathPrefix` set, a prefix most of its calls leave out (`/api/v1`) is worked out and reported. Calls that hit no route are listed under System.

## Key decisions

- **Watching costs no tokens.** Claude never reads the map; the hook is a fire-and-forget `curl`. ([ADR 0001](decisions/0001-zero-token-hooks.md))
- **Read code, don't run it.** No app has to start; a cold index of a 638-file, six-app project took 1.7 s. ([ADR 0002](decisions/0002-static-parsing.md))
- **Function-level diffs.** Each function's body is hashed at parse time, so edits are reported per function and their callers can be traced. ([ADR 0003](decisions/0003-function-level-diffs.md))
- **Questions drawn as flows.** When Claude only reads, Live draws one row per flow it followed, instead of a file graph. ([ADR 0004](decisions/0004-question-mode-strands.md))
- **One adapter per framework, and say what wasn't read.** Framework rules live in small adapter files; a coverage report names what was found and what wasn't. ([ADR 0005](decisions/0005-adapters-and-coverage.md))
- **Layout from the links.** ELK puts a caller left of what it calls and keeps lines from crossing; an app is browsed a folder at a time. ([ADR 0007](decisions/0007-layered-layout.md))
- **Plain English on top, never instead.** Simple mode asks `claude -p` to name files, functions and flow steps, caches each name against the function's hash, and never lets the model add or move a link. ([ADR 0006](decisions/0006-simplify-layer.md))

## Known limits

- Calls made through strings, dependency injection, Redux `dispatch` or websocket managers can't be followed statically. They're counted and listed under "Calls the map could not follow" in the System view.
- Only Python and TypeScript/JavaScript are parsed. HTTP matching assumes a backend whose routes are declared with FastAPI-style decorators.
- Live sessions are kept in memory; restarting the server clears them (the code index is cached). Folders mapped on the fly are mapped again when Claude next works in them.
- A folder is mapped on the fly only up to 5,000 code files, and never the home folder; bigger projects need a config.
- "Who held the thread pool during an incident" needs runtime data. The Resources view shows who *could* be involved, not who was.
