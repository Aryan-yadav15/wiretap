"""Local web server: serves the map, watches files, receives Claude Code hooks, streams updates (SSE)."""

import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import ask as asker
from .graph import Project, autodetect
from .coverage import lines as coverage_lines
from .session import Sessions
from .simplify import DescribeError, Describer, cut_steps, jobs_for, layer_of

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
CALL_KINDS = {"call", "http", "renders"}
LINK_WORDS = {"call": ("calls", "called by"), "http": ("calls the API in", "serves an API call from"),
              "renders": ("renders", "rendered by"), "db": ("uses table", "used by")}
MAX_DESCRIBE = 24  # files queued per request, in the order given (what's in focus first)


class GraphIndex:
    """Reverse adjacency for impact questions (who calls this?)."""

    def __init__(self, g):
        self.callers = defaultdict(set)
        self.fn_file = {f["id"]: f["file"] for f in g["fns"]}
        node_file = {n["id"]: n.get("file") for n in g["nodes"]}
        # files next to each other, and how: a call either way, an API call, rendering, an import or load, a stylesheet
        # whose classes it uses, or a table both use (tables are their own nodes). near[a][b] says it from a's side.
        self.near = defaultdict(dict)
        for e in g["edges"]:
            if e["k"] in CALL_KINDS:
                self.callers[e["t"]].add(e["s"])
            if e["k"] in ("trig", "res", "ext"):
                continue
            a = self.fn_file.get(e["s"]) or node_file.get(e["s"])
            b = e["t"] if e["t"].startswith("tb:") else self.fn_file.get(e["t"]) or node_file.get(e["t"])
            if a and b and a != b:
                k = LINK_WORDS.get(e["k"], ("uses", "used by"))
                self.near[a].setdefault(b, k[0])
                self.near[b].setdefault(a, k[1])
        for key, words in (("imports", ("imports", "imported by")), ("styles", ("styled by", "styles"))):
            for a, targets in g.get(key, {}).items():
                w = ("loads", "loaded by") if a.endswith((".html", ".css")) else words  # a page's <script>, a sheet's @import
                for b in targets:
                    self.near[a].setdefault(b, w[0])
                    self.near[b].setdefault(a, w[1])

    def upstream(self, fn_ids, depth=3, exclude_files=()):
        seen, frontier = set(fn_ids), set(fn_ids)
        for _ in range(depth):
            nxt = set()
            for f in frontier:
                nxt |= self.callers.get(f, set()) - seen
            seen |= nxt
            frontier = nxt
        fns = {f for f in seen - set(fn_ids) if self.fn_file.get(f) not in exclude_files}
        return fns, {self.fn_file[f] for f in fns if f in self.fn_file}

    def stray(self, files, depth=2):
        """Changed files with no call, import, load, stylesheet or shared table linking them to any other changed
        file within `depth` hops: usually edits unrelated to the rest of the change."""
        # a package marker (__init__.py) is needed without anything calling into it
        files = {f for f in files if not f.endswith("/__init__.py")}
        if len(files) < 2:
            return set()
        return {f for f, found in self.links(files, depth).items() if not found}

    def links(self, files, depth=2):
        """For each changed file, the other changed files it's linked to within `depth` hops, and how:
        {file: [[other, "calls"], [other, "via orders.py"]]}. A file with none gets an empty list."""
        files = set(files)
        out = {}
        for f in files:
            found, seen, frontier = [], {f}, {f: None}  # frontier: file -> the first hop on the way to it
            for _ in range(depth):
                nxt = {}
                for x, first in frontier.items():
                    for n, how in self.near.get(x, {}).items():
                        if n in seen:
                            continue
                        seen.add(n)
                        nxt[n] = first or n
                        if n in files:
                            found.append([n, how if not first else "via " + _short(first)])
                frontier = nxt
            out[f] = found
        return out


def _short(fid):
    """f:app:web/app.js -> app.js; tb:app:orders -> table orders"""
    return "table " + fid.rsplit(":", 1)[-1] if fid.startswith("tb:") else fid.rsplit("/", 1)[-1].rsplit(":", 1)[-1]

TOOL = os.path.dirname(WEB)


def _under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class ProjectState:
    """One mapped project: its index, graph, sessions and file text."""

    def __init__(self, hub, pid, project, auto=False, note=None):
        self.hub, self.id, self.project, self.auto, self.note = hub, pid, project, auto, note
        self.lock = hub.lock
        self.project.load_cache()
        self.project.scan()
        self.project.save_cache()
        self.graph = self._build()
        self.index = GraphIndex(self.graph)
        self.graph_json = json.dumps(self.graph, separators=(",", ":")).encode()
        self.sessions = Sessions(self.project)
        self.text_cache = {}
        self.describer = Describer(self.project.cache_dir)

    def _build(self):
        g = self.project.build()
        g.update(toolDir=TOOL, root=self.project.root, project=self.id, auto=self.auto, note=self.note)
        return g

    def info(self):
        last = max((x.last for x in self.sessions.by_id.values()), default=0)
        return {"id": self.id, "name": self.graph["name"], "root": self.project.root, "auto": self.auto, "note": self.note,
                "files": self.graph["stats"]["files"], "sessions": len(self.sessions.by_id), "last": last}

    # ---- graph updates ----
    def rebuild(self):
        self.graph = self._build()
        self.index = GraphIndex(self.graph)
        self.graph_json = json.dumps(self.graph, separators=(",", ":")).encode()
        self.text_cache.clear()
        self.hub.broadcast({"type": "graph", "project": self.id, "version": self.graph["version"]})

    def rescan_one(self, app, rel):
        """Re-extract one file now (called from a hook). Returns (old, new)."""
        old = app.extract.get(rel)
        changes = self.project.scan(only={(app.id, rel)})
        for app_id, r, o, n in changes:
            if app_id == app.id and r == rel:
                return o, n
        return old, app.extract.get(rel)

    def on_hook(self, payload):
        with self.lock:
            changed = self.sessions.on_hook(payload, self.rescan_one)
            if changed and payload.get("tool_name") in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                self.project.save_cache()
                self.rebuild()
            if changed:
                self.push_session(self.sessions.get(payload.get("session_id") or "unknown"))

    def snapshot(self, sess):
        return {**self.sessions.snapshot(sess, self.index), "project": self.id}

    def push_session(self, sess):
        self.hub.broadcast({"type": "session", "project": self.id, "session": self.snapshot(sess)})

    def tick(self):
        """One watcher pass: pick up edits made outside Claude."""
        with self.lock:
            changes = self.project.scan()
            if not changes:
                return
            for app_id, rel, old, new in changes:
                self.sessions.on_file_change(self.project.app_by_id[app_id], rel, old, new)
            self.project.save_cache()
            self.rebuild()
            latest = self.sessions.latest()
            if latest:
                self.push_session(latest)

    # ---- helpers for endpoints ----
    def file_text(self, file_id):
        if file_id in self.text_cache:
            return self.text_cache[file_id]
        _, app_id, rel = file_id.split(":", 2)
        app = self.project.app_by_id.get(app_id)
        if not app:
            return None
        try:
            with open(os.path.join(app.root, rel), encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return None
        self.text_cache[file_id] = lines
        return lines

    def search(self, terms):
        """Feature trace: functions whose code mentions any term, plus what they connect to."""
        terms = [t.strip() for t in terms if t.strip()]
        if not terms:
            return {"hits": [], "terms": []}
        rx = term_regex(terms)
        g = self.graph
        fns_by_file = defaultdict(list)
        for f in g["fns"]:
            fns_by_file[f["file"]].append(f)
        hits = {}
        file_hits = defaultdict(int)
        for fi in g["files"]:
            lines = self.file_text(fi["id"])
            if not lines:
                continue
            fns = sorted(fns_by_file.get(fi["id"], []), key=lambda f: (f["end"] - f["line"]))  # innermost first
            for i, line in enumerate(lines, 1):
                m = rx.search(line)
                if not m:
                    continue
                file_hits[fi["id"]] += 1
                owner = next((f for f in fns if f["line"] <= i <= f["end"]), None)
                key = owner["id"] if owner else fi["id"]
                h = hits.setdefault(key, {"id": key, "count": 0, "terms": set(), "lines": []})
                h["count"] += 1
                h["terms"].add(m.group(0).lower())
                if len(h["lines"]) < 3:
                    h["lines"].append([i, line.strip()[:160]])
        hit_ids = set(hits)
        # connected by data: other functions touching tables that are specific to this feature
        # (a table everything uses, like orders, would pull in the whole app)
        users = defaultdict(set)
        for e in g["edges"]:
            if e["t"].startswith("tb:") and e["s"].startswith("fn:"):
                users[e["t"]].add(e["s"])
        tables = set()
        for t, us in users.items():
            n_hit = len(us & hit_ids)
            if n_hit and (n_hit / len(us) >= 0.25 or len(us) <= 6):
                tables.add(t)
        by_data = {f for t in tables for f in users[t] if f not in hit_ids}
        # connected by calls: direct callers/callees
        by_call = set()
        for e in g["edges"]:
            if e["k"] in CALL_KINDS:
                if e["s"] in hit_ids and e["t"] not in hit_ids and e["t"].startswith("fn:"):
                    by_call.add(e["t"])
                if e["t"] in hit_ids and e["s"] not in hit_ids and e["s"].startswith("fn:"):
                    by_call.add(e["s"])
        return {
            "terms": terms,
            "hits": [{**h, "terms": sorted(h["terms"])} for h in sorted(hits.values(), key=lambda h: -h["count"])],
            "tables": sorted(tables), "by_data": sorted(by_data)[:300], "by_call": sorted(by_call - by_data)[:300],
            "file_hits": dict(file_hits),
        }

    def summarize(self, sid, task_idx):
        """On-demand natural-language summary via `claude -p` (costs tokens only when clicked)."""
        with self.lock:
            sess = self.sessions.by_id.get(sid)
            if not sess or not sess.tasks:
                return {"error": "No such session"}
            task = sess.tasks[task_idx if task_idx is not None else -1]
            view = self.sessions.task_view(task, self.index)
            files = list(task.touched)
        if not files:
            return {"text": "Nothing was changed in this request, so there is nothing to summarize."}
        chunks = []
        for fid in files:
            _, app_id, rel = fid.split(":", 2)
            app = self.project.app_by_id.get(app_id)
            if not app:
                continue
            diff = _git(app.root, ["diff", "--no-color", "HEAD", "--", rel])
            if not diff.strip():
                lines = self.file_text(fid) or []
                diff = f"(new or untracked file {rel})\n" + "\n".join(lines[:250])
            chunks.append(f"### {app.name}: {rel}\n{diff}")
        body = "\n\n".join(chunks)[:60000]
        s = view["summary"]
        prompt = (
            "Summarize these code changes for the developer in under 150 words. Plain sentences, then a short list of "
            "what to double-check. Facts measured by a static analyzer: "
            f"{s['edited']} files edited, {s['created']} created, functions {json.dumps(s['fns'])}, "
            f"{s['ripple_files']} other files call changed code, off-plan files: {[f.split(':', 2)[2] for f in s['off_plan']]}, "
            f"tests: {json.dumps(s['tests'])}. The user's request was: {json.dumps(task.prompt)}.\n\nDiff follows on stdin."
        )
        env = dict(os.environ, WIRETAP_SKIP="1")
        try:
            proc = subprocess.run(["claude", "-p", "--model", "haiku", prompt], input=body, capture_output=True, text=True,
                                  timeout=180, env=env, cwd=os.path.dirname(WEB))
        except FileNotFoundError:
            return {"error": "The claude command was not found on PATH."}
        except subprocess.TimeoutExpired:
            return {"error": "claude -p took longer than 3 minutes."}
        if proc.returncode != 0:
            return {"error": (proc.stderr or proc.stdout)[-800:]}
        return {"text": proc.stdout.strip()}


    # ---- plain English (ADR 0006): the parser owns the structure, a model only names it ----
    def _apps(self):
        return {a["id"]: a["name"] for a in self.graph["apps"]}

    def _pushed(self, fid, res, err):
        msg = {"type": "describe", "project": self.id, "file": fid}
        msg.update(res or {"error": err})
        self.hub.broadcast(msg)

    def describe(self, file_ids):
        """Queue files for a description; each one is pushed over SSE when it lands."""
        with self.lock:
            jobs = jobs_for(self.graph, list(dict.fromkeys(file_ids))[:MAX_DESCRIBE], self.file_text, self._apps(), self.describer)
        return {"queued": self.describer.describe_async(jobs, self._pushed)}

    def flow(self, ids):
        """Name a flow: steps are cut where the layer changes, then one call names them."""
        with self.lock:
            g = self.graph
            role = {f["id"]: f["role"] for f in g["files"]}
            fns = {f["id"]: f for f in g["fns"]}
            nodes = {n["id"]: n for n in g["nodes"]}
            items = []
            for nid in ids:
                if nid in fns:
                    f = fns[nid]
                    items.append({"id": nid, "layer": layer_of(nid, role.get(f["file"]), None), "file": f["file"], "raw": f["name"], "hash": f["hash"]})
                elif nid in nodes:
                    n = nodes[nid]
                    items.append({"id": nid, "layer": layer_of(nid, None, n.get("kind")), "label": n.get("label") or nid, "hash": ""})
            files = list(dict.fromkeys(it["file"] for it in items if "file" in it))
            jobs = jobs_for(g, files[:MAX_DESCRIBE], self.file_text, self._apps(), self.describer)
        if not items:
            return {"error": "Nothing in this flow is on the map."}
        try:
            for job in jobs:  # outside the lock: a model call takes seconds
                self._pushed(job["file"]["id"], self.describer.describe_file(job), None)
            known = self.describer.fresh(self.graph)["fns"]
            for it in items:
                if "file" in it:
                    d = known.get(it["id"])
                    it["label"], it["line"] = (d["n"], d["t"]) if d else (it["raw"], "")
            steps = cut_steps(items)
            named = self.describer.flow(steps)
        except DescribeError as e:
            return {"error": str(e)}
        return {"title": named["title"], "steps": [{"name": nm, "layer": st["layer"], "ids": [it["id"] for it in st["items"]]}
                                                   for nm, st in zip(named["steps"], steps)]}


class State:
    """Every project Wiretap maps. The one from the config file comes first; a Claude Code session in any other
    folder gets that folder mapped on its first hook (see graph.autodetect)."""

    def __init__(self, config=None):
        self.lock = threading.RLock()
        self.projects = {}
        self.session_project = {}   # Claude session id -> project id
        self.clients = []
        self.hooks = queue.Queue()
        self.primary = self.add(Project(config)) if config else None

    # the config's project, for callers that know only one (tests, the CLI)
    sessions = property(lambda self: self.primary.sessions)
    index = property(lambda self: self.primary.index)
    graph = property(lambda self: self.primary.graph)
    project = property(lambda self: self.primary.project)

    def add(self, project, auto=False, note=None):
        base = re.sub(r"[^a-z0-9]+", "-", os.path.basename(project.root).lower()).strip("-") or "project"
        pid, n = base, 2
        while pid in self.projects:
            pid, n = f"{base}-{n}", n + 1
        ps = self.projects[pid] = ProjectState(self, pid, project, auto, note)
        return ps

    def get(self, pid=None):
        return self.projects.get(pid) or self.primary or self.latest_project()

    def latest_project(self):
        return max(self.projects.values(), key=lambda p: p.info()["last"], default=None)

    def for_cwd(self, cwd):
        """The project a folder belongs to, mapping it first if no project covers it."""
        if not cwd:
            return self.get()
        cwd = os.path.normpath(cwd)
        inside = [p for p in self.projects.values() if _under(cwd, p.project.root)]
        if inside:
            return max(inside, key=lambda p: len(p.project.root))
        root = _git(cwd, ["rev-parse", "--show-toplevel"]).strip() or cwd
        cfg, note = autodetect(root)
        t0 = time.time()
        pid = re.sub(r"[^a-z0-9]+", "-", os.path.basename(root).lower()).strip("-") or "project"
        ps = self.add(Project(cfg=cfg, cache_dir=os.path.join(TOOL, ".wiretap-cache", "auto", pid)), auto=True, note=note)
        st = ps.graph["stats"]
        print(f"Mapped {root}: {st['files']} files, {st['functions']} functions in {time.time() - t0:.1f}s" + (f" ({note})" if note else ""))
        print("\n".join(coverage_lines(ps.graph["coverage"])))
        self.broadcast({"type": "projects"})
        return ps

    def hook_worker(self):
        # one worker keeps hook events in the order Claude Code sent them
        while True:
            _safe(self.on_hook, self.hooks.get())

    def on_hook(self, payload):
        sid = payload.get("session_id") or "unknown"
        with self.lock:
            ps = self.projects.get(self.session_project.get(sid))
            if not ps:
                ps = self.for_cwd(payload.get("cwd")) if payload.get("cwd") or not self.primary else self.primary
                self.session_project[sid] = ps.id
        ps.on_hook(payload)

    def find_session(self, sid):
        for ps in self.projects.values():
            if sid in ps.sessions.by_id:
                return ps, ps.sessions.by_id[sid]
        return None, None

    def all_sessions(self):
        out = [(ps, x) for ps in self.projects.values() for x in ps.sessions.by_id.values()]
        return sorted(out, key=lambda px: -px[1].last)

    def watch(self, interval=1.0):
        while True:
            time.sleep(interval)
            for ps in list(self.projects.values()):
                try:
                    ps.tick()
                except Exception as e:  # keep watching even if one file breaks the parser
                    print("watch error:", e)

    # ---- SSE ----
    def broadcast(self, msg):
        data = ("data: " + json.dumps(msg, separators=(",", ":")) + "\n\n").encode()
        for q in list(self.clients):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


def term_regex(terms):
    """Match feature words as identifier parts: 'cod' finds cod, COD, isCod, cod_fee, but not code or decode."""
    alts = []
    for t in terms:
        if re.search(r"[_A-Z]", t[1:]) or len(t) > 6:
            alts.append(r"(?i:" + re.escape(t) + ")")
            continue
        low, up, cap = t.lower(), t.upper(), t[:1].upper() + t[1:].lower()
        alts.append(r"(?<![A-Za-z0-9])" + re.escape(low) + r"(?![a-z0-9])")
        alts.append(r"(?<![A-Za-z0-9])" + re.escape(up) + r"(?![A-Za-z0-9])")
        alts.append(r"(?<![A-Z0-9])" + re.escape(cap) + r"(?![a-z0-9])")
    return re.compile("|".join(alts))


def _git(cwd, args):
    try:
        return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, separators=(",", ":")).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path
            if path == "/" or path.startswith("/web/") or path in ("/app.js", "/style.css"):
                name = "index.html" if path == "/" else path[len("/web/"):] if path.startswith("/web/") else path[1:]
                fp = os.path.normpath(os.path.join(WEB, name))
                ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}.get(name.rsplit(".", 1)[-1])
                # web/ and its folders (vendor/elk) only, never a path that climbs out
                if not ctype or not fp.startswith(WEB + os.sep) or not os.path.isfile(fp):
                    return self._send(404, {"error": "not found"})
                ctype += "; charset=utf-8"
                with open(fp, "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            pid = (q.get("p") or [None])[0]
            if path == "/api/projects":
                with state.lock:
                    return self._send(200, {"projects": [p.info() for p in state.projects.values()],
                                            "primary": state.primary and state.primary.id})
            if path == "/api/graph":
                with state.lock:
                    ps = state.get(pid)
                    if not ps:
                        return self._send(200, {"empty": True})
                    return self._send(200, ps.graph_json)
            if path == "/api/session":
                # a session id finds its own project; otherwise the latest Claude session in the asked-for project
                with state.lock:
                    sid = (q.get("id") or [None])[0]
                    ps, sess = state.find_session(sid) if sid else (None, None)
                    if not sess:
                        ps = state.get(pid)
                        sess = ps and ps.sessions.latest()
                    return self._send(200, {
                        "session": ps.snapshot(sess) if sess else None,
                        "sessions": [{"id": x.id, "source": x.source, "last": x.last, "project": p.id, "projectName": p.graph["name"],
                                      "prompt": next((t.prompt for t in reversed(x.tasks) if t.prompt), None)}
                                     for p, x in state.all_sessions()],
                    })
            if path == "/api/source":
                fid = (q.get("file") or [""])[0]
                start = int((q.get("start") or ["1"])[0])
                end = int((q.get("end") or [str(start + 40)])[0])
                with state.lock:
                    lines = state.get(pid).file_text(fid)
                if lines is None:
                    return self._send(404, {"error": "file not found"})
                end = min(end, start + 400, len(lines))
                return self._send(200, {"file": fid, "start": start, "lines": lines[start - 1:end]})
            if path == "/api/describe":
                with state.lock:
                    ps = state.get(pid)
                    return self._send(200, ps.describer.fresh(ps.graph) if ps else {"fns": {}, "files": {}})
            if path == "/api/search":
                terms = re.split(r"[,\n]", (q.get("q") or [""])[0])
                with state.lock:
                    return self._send(200, state.get(pid).search(terms))
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                cq = queue.Queue(maxsize=200)
                state.clients.append(cq)
                try:
                    self.wfile.write(b": hello\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            data = cq.get(timeout=15)
                        except queue.Empty:
                            data = b": ping\n\n"
                        self.wfile.write(data)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    if cq in state.clients:
                        state.clients.remove(cq)
                return
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if u.path == "/api/hook":
                # answer immediately so Claude Code never waits on us
                self._send(200, {"ok": True})
                state.hooks.put(body)
                return
            if u.path == "/api/summarize":
                ps, _ = state.find_session(body.get("session"))
                if not ps:
                    return self._send(200, {"error": "No such session"})
                return self._send(200, ps.summarize(body.get("session"), body.get("task")))
            if u.path == "/api/ask":
                return self._ask(body)
            if u.path in ("/api/describe", "/api/flow"):
                ps = state.get(body.get("p"))
                if not ps:
                    return self._send(200, {"error": "No project is mapped yet."})
                if u.path == "/api/describe":
                    return self._send(200, ps.describe([f for f in body.get("files") or [] if isinstance(f, str)]))
                return self._send(200, ps.flow([i for i in body.get("ids") or [] if isinstance(i, str)][:40]))
            return self._send(404, {"error": "not found"})

        def _ask(self, body):
            """Stream an answer as one JSON event per line, until it's done or the browser stops listening."""
            q = (body.get("q") or "").strip()
            if not q:
                return self._send(400, {"error": "empty question"})
            with state.lock:
                ps = state.get(body.get("p"))
                root = ps.project.root if ps else os.path.dirname(WEB)
                system = asker.system_prompt(ps.graph if ps else {}, str(body.get("focus") or "")[:200] or None)
            resume = body.get("session")
            resume = resume if isinstance(resume, str) and re.fullmatch(r"[0-9a-f-]{36}", resume) else None
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            events = asker.ask(q[:8000], root, system, resume)
            try:
                for ev in events:
                    self.wfile.write(json.dumps(ev).encode() + b"\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                events.close()

    return H


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("hook error:", e)


def serve(config, port=None, host="127.0.0.1"):
    t0 = time.time()
    state = State(config)
    port = port or (state.project.cfg.get("port", 7878) if state.primary else 7878)
    threading.Thread(target=state.watch, daemon=True).start()
    threading.Thread(target=state.hook_worker, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    srv.daemon_threads = True
    if state.primary:
        st = state.graph["stats"]
        print(f"Wiretap: {st['files']} files, {st['functions']} functions, {st['edges']} links indexed in {time.time() - t0:.1f}s")
        print("\n".join(coverage_lines(state.graph["coverage"])))
    else:
        print("Wiretap: no config, so it maps whichever project Claude Code works in")
    print(f"Open http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return state
