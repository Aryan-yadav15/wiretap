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

from .graph import Project
from .session import Sessions

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
CALL_KINDS = {"call", "http", "renders"}


class GraphIndex:
    """Reverse adjacency for impact questions (who calls this?)."""

    def __init__(self, g):
        self.callers = defaultdict(set)
        self.fn_file = {f["id"]: f["file"] for f in g["fns"]}
        for e in g["edges"]:
            if e["k"] in CALL_KINDS:
                self.callers[e["t"]].add(e["s"])

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


class State:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.project = Project(config)
        self.project.load_cache()
        self.project.scan()
        self.project.save_cache()
        self.graph = self._build()
        self.index = GraphIndex(self.graph)
        self.graph_json = json.dumps(self.graph, separators=(",", ":")).encode()
        self.sessions = Sessions(self.project)
        self.clients = []
        self.text_cache = {}
        self.hooks = queue.Queue()

    def hook_worker(self):
        # one worker keeps hook events in the order Claude Code sent them
        while True:
            _safe(self.on_hook, self.hooks.get())

    def _build(self):
        g = self.project.build()
        g["toolDir"] = os.path.dirname(WEB)
        return g

    # ---- graph updates ----
    def rebuild(self):
        self.graph = self._build()
        self.index = GraphIndex(self.graph)
        self.graph_json = json.dumps(self.graph, separators=(",", ":")).encode()
        self.text_cache.clear()
        self.broadcast({"type": "graph", "version": self.graph["version"]})

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

    def push_session(self, sess):
        self.broadcast({"type": "session", "session": self.sessions.snapshot(sess, self.index)})

    def watch(self, interval=1.0):
        while True:
            time.sleep(interval)
            try:
                with self.lock:
                    changes = self.project.scan()
                    if not changes:
                        continue
                    for app_id, rel, old, new in changes:
                        self.sessions.on_file_change(self.project.app_by_id[app_id], rel, old, new)
                    self.project.save_cache()
                    self.rebuild()
                    latest = self.sessions.latest()
                    if latest:
                        self.push_session(latest)
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
                name = "index.html" if path == "/" else os.path.basename(path)
                fp = os.path.join(WEB, name)
                if not os.path.isfile(fp):
                    return self._send(404, {"error": "not found"})
                ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]] + "; charset=utf-8"
                with open(fp, "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            if path == "/api/graph":
                with state.lock:
                    return self._send(200, state.graph_json)
            if path == "/api/session":
                with state.lock:
                    sid = (q.get("id") or [None])[0]
                    sess = state.sessions.by_id.get(sid) if sid else state.sessions.latest()
                    if not sess:
                        return self._send(200, {"session": None, "sessions": []})
                    return self._send(200, {
                        "session": state.sessions.snapshot(sess, state.index),
                        "sessions": [{"id": s.id, "source": s.source, "last": s.last,
                                      "prompt": next((t.prompt for t in reversed(s.tasks) if t.prompt), None)}
                                     for s in sorted(state.sessions.by_id.values(), key=lambda s: -s.last)],
                    })
            if path == "/api/source":
                fid = (q.get("file") or [""])[0]
                start = int((q.get("start") or ["1"])[0])
                end = int((q.get("end") or [str(start + 40)])[0])
                with state.lock:
                    lines = state.file_text(fid)
                if lines is None:
                    return self._send(404, {"error": "file not found"})
                end = min(end, start + 400, len(lines))
                return self._send(200, {"file": fid, "start": start, "lines": lines[start - 1:end]})
            if path == "/api/search":
                terms = re.split(r"[,\n]", (q.get("q") or [""])[0])
                with state.lock:
                    return self._send(200, state.search(terms))
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
                return self._send(200, state.summarize(body.get("session"), body.get("task")))
            return self._send(404, {"error": "not found"})

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
    port = port or state.project.cfg.get("port", 7878)
    threading.Thread(target=state.watch, daemon=True).start()
    threading.Thread(target=state.hook_worker, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    srv.daemon_threads = True
    st = state.graph["stats"]
    print(f"Wiretap: {st['files']} files, {st['functions']} functions, {st['edges']} links indexed in {time.time() - t0:.1f}s")
    print(f"Open http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return state
