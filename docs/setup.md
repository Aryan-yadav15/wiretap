# Setup

## Requirements

| Tool | Version (tested) | Why |
|---|---|---|
| Python | 3.11+ (tests pass on 3.11, 3.12, 3.13, 3.15) | the server |
| Node.js | 24.16 | runs the TypeScript parser |
| TypeScript | 5.9.3 (pinned in `package.json`) | parsing `.ts` / `.tsx` |
| curl | any | the Claude Code hooks |
| Claude Code | optional | the Live view and the terminal button |

## Run locally

```bash
git clone https://github.com/Aryan-yadav15/wiretap && cd wiretap
npm install
cp wiretap.config.example.json wiretap.config.json   # then edit it, see below
python3 -m wiretap serve --open
```

Check what was found without starting the server:

```bash
python3 -m wiretap index
```

It prints files and functions per app, how many API calls matched a route, any that didn't, and how many calls couldn't be followed.

## Connect Claude Code

```bash
python3 -m wiretap install-hooks             # backs up ~/.claude/settings.json first
python3 -m wiretap install-hooks --dry-run   # print the settings instead of writing them
python3 -m wiretap uninstall-hooks           # remove everything it added
```

`install-hooks` adds the hooks (each marked `# wiretap`) and puts `claude-mod/wiretap-live` in the `CLAUDE_CODE_PLUGIN_DIRS` setting, which loads the terminal button. Start a new Claude Code session afterwards. The hooks do nothing when the server isn't running; the button starts it.

## Configuration

Wiretap reads a JSON config file (default `wiretap.config.json` next to the `wiretap/` folder) and three environment variables.

### Config file

| Key | Required / default | Example | Purpose | Read in |
|---|---|---|---|---|
| `name` | no | `"My project"` | title in the UI | [`graph.py`](../wiretap/graph.py#L230) |
| `root` | no, `"."` | `"../my-project"` | project folder, relative to the config file | [`graph.py:103`](../wiretap/graph.py#L103) |
| `port` | no, `7878` | `7878` | server port (`serve --port` overrides it) | [`server.py:380`](../wiretap/server.py#L380) |
| `typescript` | no | `"web/node_modules/typescript"` | a TypeScript install to use, relative to `root` | [`graph.py:106`](../wiretap/graph.py#L106) |
| `hosts` | no | `{"api.stripe.com": "Stripe"}` | extra URL hosts to show as outside services | [`graph.py:110`](../wiretap/graph.py#L110) |
| `externalsPython` / `externalsJs` | no | `{"stripe": "Stripe"}` | extra SDK modules to show as outside services | [`graph.py:108`](../wiretap/graph.py#L108) |
| `apps` | **yes** | see below | the apps in the project | [`graph.py:104`](../wiretap/graph.py#L104) |

Each entry in `apps`:

| Key | Required / default | Example | Purpose |
|---|---|---|---|
| `id` | **yes** | `"web"` | short id used in links |
| `name` | no, the id | `"Web app"` | label in the UI |
| `lang` | **yes** | `"ts"` or `"py"` | which parser to use |
| `root` | **yes** | `"frontend"` | app folder, relative to the project `root` |
| `include` | no, `["."]` | `["src", "App.tsx"]` | folders or files inside the app to read |
| `skipDirs` | no | `["generated"]` | extra folders to skip (`node_modules`, `dist`, `build`, … are always skipped) |
| `httpClients` | no | `["api"]` | names of HTTP client objects (`api.get(...)`) for a TS app |
| `backend` | no | `"api"` | which Python app those calls go to |
| `pathPrefix` | no, `""` | `"/api/v1"` | prefix the client adds before each path |
| `alias` | no | `{"@/": "src/"}` | import aliases |
| `screenDirs` | no, `["/pages/", "/screens/"]` | `["/routes/"]` | folders whose components count as screens |
| `roles` | no | `{"src/lib/": "client"}` | force the lane of files under a path |
| `db` | no | `{"id": "pg", "name": "Postgres", "client": "Database client (shared)"}` | for a Python app: its database and the name of its shared client |
| `fstring` | no | `{"settings.API_VERSION": "v1"}` | values for names used inside f-string route prefixes |

### Environment variables

| Name | Required / default | Example | Purpose | Read in |
|---|---|---|---|---|
| `WIRETAP_CONFIG` | no, `wiretap.config.json` | `~/configs/shop.json` | config file to use (same as `--config`) | [`__main__.py`](../wiretap/__main__.py) `main` |
| `WIRETAP_TYPESCRIPT` | no | `/usr/local/lib/node_modules/typescript` | TypeScript install to use, checked first | [`graph.py:194`](../wiretap/graph.py#L194) |
| `WIRETAP_SKIP` | no | `1` | when set in Claude Code's environment, the hooks send nothing (the summary's own `claude -p` run sets it) | [`__main__.py:25`](../wiretap/__main__.py#L25), [`server.py:228`](../wiretap/server.py#L228) |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No config at …/wiretap.config.json` | no config yet | `cp wiretap.config.example.json wiretap.config.json` and edit it |
| `Folder not found: …` | `root` or an app's `root` in the config points nowhere (for example the unedited example config) | fix the paths; `root` is relative to the config file, app roots to `root` |
| `<app>: TypeScript not found` | no TypeScript where Wiretap looks | run `npm install` in the Wiretap folder, or set `typescript` / `WIRETAP_TYPESCRIPT` |
| Many "API calls with no matching route" in System | wrong `backend` or `pathPrefix`, or routes declared in a way Wiretap doesn't read | check `pathPrefix` against what the client adds; `python3 -m wiretap index` lists each unmatched call with its file and line |
| Live stays on "Waiting for this Claude session" | hooks not installed, or the session started before installing | `python3 -m wiretap install-hooks`, then start a new Claude Code session |
| The button doesn't appear above the prompt | the session started before installing, or Claude Code asked to load the plugin and it was declined | start a new session and allow `wiretap-live` |
| Port already in use | another server on 7878 | `python3 -m wiretap serve --port 7900`, and `install-hooks --port 7900` so the hooks follow. The terminal button always opens port 7878. |
