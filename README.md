# Wiretap

Listen in on Claude Code while it works on your codebase, and see how that codebase connects.

Wiretap draws a live map of a project: apps → API routes → backend logic → tables → outside services. Drill down from apps to files to single functions, see what uses something and what it affects, trace a feature across every app, and watch Claude Code's reads and edits light up on the map as they happen. When Claude only answers a question, it shows the flows Claude followed to find the answer.

It reads Python (FastAPI, Supabase, SQLAlchemy) and TypeScript / React / React Native. Everything runs locally and costs no tokens.

## Start

Needs Python 3.10+ and Node.

```bash
git clone https://github.com/Aryan-yadav15/wiretap && cd wiretap
npm install                                         # TypeScript, used to parse .ts/.tsx
cp wiretap.config.example.json wiretap.config.json  # then point it at your project (below)
python3 -m wiretap serve --open                     # http://127.0.0.1:7878
```

On a project of about 640 files, the first index takes about 2 seconds. After that it is cached in `.wiretap-cache/` and the server re-reads only the files that change (it checks every second, which costs about 3 ms).

## Connect Claude Code (one time)

```bash
python3 -m wiretap install-hooks      # adds hooks to ~/.claude/settings.json (backup: settings.json.before-wiretap)
python3 -m wiretap install-hooks --dry-run   # show what would be written
python3 -m wiretap uninstall-hooks    # remove them again
```

The hooks are one `curl` to `127.0.0.1:7878` per tool call. They cost **no tokens**, finish in milliseconds, and do nothing when the server isn't running. Claude never reads the map. The map only watches.

Start a new Claude Code session after installing, then open the **Live** tab.

The same command adds a **◉ Wiretap live ↗** button above the Claude Code prompt (and a `/wiretap` command). Press it to open that session's Live view in your browser. If the server isn't running, it starts it first. Neither one uses tokens. The button is a Claude Code mod in `claude-mod/wiretap-live`, loaded through `CLAUDE_CODE_PLUGIN_DIRS` in your settings.

## Views

| View | What it answers |
|---|---|
| **System** | Which apps talk to which backends, databases and outside services, with counts. Also shows how complete the map is (unmatched API calls, calls it could not follow). |
| **Map** | Focus on one file, table, service or resource. Amber = uses it, teal = it reaches. Click a box to move, click the focused box to look inside, click a line to see the exact calls behind it. Depth 1–3. |
| **Inside a file** | Functions in the file, who calls in and what they call out to. Click a function: its callers, its calls, **where the flow starts** (button tap, screen opening, API route, startup or scheduled job), how many files sit upstream, and its code. |
| **Live** | The Claude session as it happens: files read, the plan (dashed), files created and edited (with the number of functions changed), files that call changed code (amber), edits **outside the plan** (red !), test results, and a timeline. The summary is computed by the map (0 tokens). **Summarize with Claude** runs `claude -p --model haiku` on the git diff, only when clicked. Your own edits show up under "Your own edits". When Claude only answers a question (reads, changes nothing), Live draws **one row per flow** Claude followed, left to right from where it starts (button, screen, route) to the tables it touches, with ①②③ for the order it looked. Flows that share a start branch off; many small calls between two files fold into one row; other reads sit under "Also read". Solid = read, dashed = only a search matched it. Click a row title to show just that flow, **▶ Replay** steps through the session, and `&step=N` in the URL freezes it at a step. Reads and searches done through the shell (`grep -rn`, `sed -n 10,40p`) count too, down to the function. |
| **Trace** | Type the words a feature uses (`cod, pay_later, payLater`). It finds every function that mentions them as an identifier (`cod` matches `isCod` and `cod_fee`, not `code`), plus functions that touch the same feature-specific tables without using the words. |
| **Resources** | Shared things that can cause a stall: the 40-thread worker pool, the shared Supabase client, the SQLAlchemy session pool. Who uses each one, and what else those functions share. |

Press `/` to search for any file, function, route or table.

## What it understands

- **Python / FastAPI**: functions, methods and nested functions; imports (including relative ones); `APIRouter(prefix=…)` and `include_router(…, prefix=…)` chains; route decorators; Supabase `.table("x").select/insert/update/…` (read vs write) and `.rpc()`; SQLAlchemy models via `__tablename__`; `run_in_threadpool`, `asyncio.to_thread`, sync `def` endpoints (these use the thread pool too); `on_event("startup")`, `create_task`, `add_job`, `BackgroundTasks.add_task`; outside SDKs (Razorpay, Firebase, …) and HTTP calls to known hosts.
- **TypeScript / React / React Native**: functions, components, handlers nested in components, object-of-functions exports (`admin.getZones`), class instances (`orderService.getMyOrders`), re-exports, `@/` aliases; `api.get/post/…` and `fetch` calls with URLs from literals, template strings and constants like `API_ENDPOINTS.X(id)`; JSX `onClick` / `onPress` handlers (with the button text); `<Component/>` renders; direct `supabase.from("table")` calls; hooks like `const { addItem } = useCart()` (marked as a guess).
- Each API call in an app is matched to its backend route (method + path, `{param}` aware). Calls that hit no route are listed under System.

## Limits

- It reads code. It doesn't run it. Calls made through strings, dependency injection or runtime config may be missing, and the map counts them under "Calls the map could not follow" (Redux `dispatch`, websocket managers, etc.).
- "Who held the thread pool during the crash" needs runtime data (request timing, `py-spy dump`, `pg_stat_activity`). The Resources view shows who *could* be involved.
- Planned files come from file names in Claude's plan. Vague plans produce fewer dashed boxes.
- The live session lives in memory. Restarting the server clears it (the code index is cached).

## Configure your project

`wiretap.config.json` (kept out of git) says where the project is and which apps it has:

| Key | Meaning |
|---|---|
| `root` | The project folder, relative to the Wiretap folder |
| `apps[].lang` | `ts` for TypeScript/JavaScript apps, `py` for Python backends |
| `apps[].root`, `include` | The app's folder inside the project, and which folders in it to read |
| `httpClients` | Names of the HTTP client objects the app calls (`api.get(...)`) |
| `backend`, `pathPrefix` | Which backend app those calls go to, and any prefix the client adds |
| `alias` | Import aliases such as `@/` → `src/` |
| `db` | For a backend: its database, and the name for its shared client |
| `typescript` | Optional: a `node_modules/typescript` folder to use instead of Wiretap's own |

A frontend talking to one FastAPI backend is in `wiretap.config.example.json`. Use another file with `python3 -m wiretap --config path/to/config.json serve`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They index a small fixture project and replay a Claude session (prompt → read → plan → edit → off-plan edit → tests → stop) through the hook handler and the HTTP API.

## Files

```
wiretap/py_extract.py   Python parser (ast)
wiretap/ts_extract.mjs  TypeScript parser (TypeScript compiler API, no type checking)
wiretap/graph.py        links everything: imports, routes, HTTP matching, tables, resources, entry points
wiretap/session.py      hook events → session timeline, function-level diffs, plan, tests
wiretap/server.py       HTTP + server-sent events, file watcher, trace search, on-demand summary
web/                    the UI (plain HTML/CSS/JS, no build step)
claude-mod/wiretap-live the terminal button + /wiretap (Claude Code mod)
```
