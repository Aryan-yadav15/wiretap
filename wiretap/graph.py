"""Build one linked graph from per-file extractions.

Node ids
  f:<app>:<rel>            file
  fn:<app>:<rel>#<qual>    function / method / component
  rt:<app>:<METHOD> <path> API route (missing=True when a client calls a path no backend defines)
  tb:<db>:<name>           database table or rpc
  ext:<name>               outside service
  res:<app>:<kind>         shared resource (thread pool, DB client)
  tg:<app>:<rel>:<line>:<n> entry point (button, screen, scheduled job, app startup)

Edges are function level: {"s", "t", "k", "l"?, "line"?, "c"?}
  k: call | http | renders | read | write | db | ext | res | trig
"""

import json
import os
import re
import subprocess
import time
from collections import defaultdict

from . import py_extract

DEFAULT_EXTERNALS_PY = {
    "razorpay": "Razorpay", "firebase_admin": "Firebase", "boto3": "AWS", "botocore": "AWS",
    "twilio": "Twilio", "sendgrid": "SendGrid", "stripe": "Stripe", "openai": "OpenAI",
    "anthropic": "Anthropic", "google.genai": "Gemini", "google.generativeai": "Gemini",
    "redis": "Redis", "aioredis": "Redis", "celery": "Celery", "sentry_sdk": "Sentry",
    "resend": "Resend", "googlemaps": "Google Maps", "pywebpush": "Web push", "exponent_server_sdk": "Expo push",
    "jwt": None, "httpx": None, "requests": None, "supabase": None,
}
DEFAULT_EXTERNALS_TS = {
    "react-native-razorpay": "Razorpay", "razorpay": "Razorpay",
    "@react-native-firebase/messaging": "Firebase", "@react-native-firebase/app": "Firebase", "firebase": "Firebase",
    "expo-notifications": "Expo push", "@sentry/react": "Sentry", "@sentry/react-native": "Sentry",
    "posthog-js": "PostHog", "posthog-react-native": "PostHog", "@supabase/supabase-js": "Supabase (direct from client)",
    "socket.io-client": "WebSocket", "@react-native-google-signin/google-signin": "Google sign-in",
    "react-native-maps": "Maps", "@googlemaps/js-api-loader": "Google Maps", "mixpanel-browser": "Mixpanel",
}
DEFAULT_HOSTS = {"razorpay.com": "Razorpay", "googleapis.com": "Google APIs", "supabase.co": "Supabase",
                 "expo.dev": "Expo push", "exp.host": "Expo push", "shadowfax": "Shadowfax", "porter": "Porter",
                 "fcm.googleapis.com": "Firebase"}

TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs")
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".git", ".expo", "android", "ios",
             "coverage", ".next", "migrations", "alembic", "__tests__", "tests", "test"}

LANES = [
    ("screen", "Screens"), ("ui", "Components"), ("client", "Client logic"), ("route", "API routes"),
    ("logic", "Backend logic"), ("data", "Data"), ("outside", "Outside"),
]


def norm_path(p):
    p = re.sub(r"/+", "/", "/" + p.strip("/")) if p else "/"
    p = p.split("?")[0]
    return p.rstrip("/") or "/"


def _seg_is_param(s):
    return "{" in s or s == "*"


class App:
    def __init__(self, cfg, root):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.lang = cfg["lang"]
        self.root = os.path.normpath(os.path.join(root, cfg["root"]))
        self.include = cfg.get("include", ["."])
        self.extract = {}   # rel -> extraction dict
        self.stamp = {}     # rel -> (mtime, size)

    def files(self):
        exts = (".py",) if self.lang == "py" else TS_EXT
        skip = SKIP_DIRS | set(self.cfg.get("skipDirs", []))
        out = []
        for inc in self.include:
            base = os.path.join(self.root, inc)
            if os.path.isfile(base):
                out.append(os.path.relpath(base, self.root))
                continue
            for dp, dns, fns in os.walk(base):
                dns[:] = [d for d in dns if d not in skip and not d.startswith(".") and not d.startswith("tmpclaude")]
                for f in fns:
                    if f.endswith(exts) and not f.endswith(".d.ts") and ".test." not in f and ".spec." not in f:
                        out.append(os.path.relpath(os.path.join(dp, f), self.root))
        return sorted(out)

    def module_of(self, rel):
        m = rel[:-3].replace(os.sep, ".")
        return m[: -len(".__init__")] if m.endswith(".__init__") else m


class Project:
    def __init__(self, config_path):
        with open(config_path) as fh:
            self.cfg = json.load(fh)
        self.config_path = config_path
        base = os.path.dirname(os.path.abspath(config_path))
        self.root = os.path.normpath(os.path.join(base, self.cfg.get("root", ".")))
        self.apps = [App(a, self.root) for a in self.cfg["apps"]]
        self.app_by_id = {a.id: a for a in self.apps}
        ts_path = self.cfg.get("typescript")
        self.typescript = os.path.normpath(os.path.join(self.root, ts_path)) if ts_path else None
        self.ext_py = {**DEFAULT_EXTERNALS_PY, **self.cfg.get("externalsPython", {})}
        self.ext_ts = {**DEFAULT_EXTERNALS_TS, **self.cfg.get("externalsJs", {})}
        self.hosts = {**DEFAULT_HOSTS, **self.cfg.get("hosts", {})}
        self.cache_dir = os.path.join(base, ".wiretap-cache")
        self.graph = None
        self.version = 0
        self.timings = {}

    # ---------- extraction (cached by mtime/size) ----------

    def locate(self, abs_path):
        """Absolute path -> (app, rel) or (None, None)."""
        p = os.path.normpath(abs_path)
        for a in sorted(self.apps, key=lambda a: -len(a.root)):
            if p.startswith(a.root + os.sep):
                return a, os.path.relpath(p, a.root)
        return None, None

    def load_cache(self):
        path = os.path.join(self.cache_dir, "extract.json")
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if data.get("config") != self.cfg:
            return
        for a in self.apps:
            entry = data.get("apps", {}).get(a.id, {})
            a.extract = entry.get("extract", {})
            a.stamp = {k: tuple(v) for k, v in entry.get("stamp", {}).items()}

    def save_cache(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        data = {"config": self.cfg, "apps": {a.id: {"extract": a.extract, "stamp": a.stamp} for a in self.apps}}
        tmp = os.path.join(self.cache_dir, "extract.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, os.path.join(self.cache_dir, "extract.json"))

    def scan(self, only=None):
        """Re-extract files whose mtime/size changed. Returns list of (app_id, rel, old, new) for changed files."""
        changed = []
        t0 = time.time()
        for a in self.apps:
            rels = a.files()
            present = set(rels)
            for gone in [r for r in a.extract if r not in present]:
                changed.append((a.id, gone, a.extract.pop(gone), None))
                a.stamp.pop(gone, None)
            todo = []
            for rel in rels:
                if only is not None and (a.id, rel) not in only:
                    if rel in a.extract:
                        continue
                try:
                    st = os.stat(os.path.join(a.root, rel))
                except OSError:
                    continue
                stamp = (st.st_mtime_ns, st.st_size)
                if a.stamp.get(rel) != stamp or rel not in a.extract:
                    todo.append((rel, stamp))
            if not todo:
                continue
            if a.lang == "py":
                fvalues = a.cfg.get("fstring", {})
                for rel, stamp in todo:
                    try:
                        with open(os.path.join(a.root, rel), encoding="utf-8", errors="replace") as fh:
                            src = fh.read()
                    except OSError:
                        continue
                    old = a.extract.get(rel)
                    a.extract[rel] = py_extract.extract(src, rel, a.module_of(rel), fvalues)
                    a.stamp[rel] = stamp
                    changed.append((a.id, rel, old, a.extract[rel]))
            else:
                results = self._run_ts(a, [r for r, _ in todo])
                for (rel, stamp), res in zip(todo, results):
                    old = a.extract.get(rel)
                    a.extract[rel] = res
                    a.stamp[rel] = stamp
                    changed.append((a.id, rel, old, res))
        self.timings["scan"] = round(time.time() - t0, 3)
        return changed

    def _find_typescript(self, app):
        cands = [os.environ.get("WIRETAP_TYPESCRIPT"),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules", "typescript")]
        cands += [os.path.join(a.root, "node_modules", "typescript") for a in [app] + self.apps if a.lang != "py"]
        try:
            npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=10).stdout.strip()
            cands.append(os.path.join(npm_root, "typescript"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        for c in cands:
            if c and os.path.isfile(os.path.join(c, "package.json")):
                self.typescript = c
                return c
        return None

    def _run_ts(self, app, rels):
        script = os.path.join(os.path.dirname(__file__), "ts_extract.mjs")
        ts_mod = app.cfg.get("typescript")
        ts_mod = os.path.normpath(os.path.join(self.root, ts_mod)) if ts_mod else (self.typescript or self._find_typescript(app))
        if not ts_mod:
            raise RuntimeError(f"{app.id}: TypeScript not found. Set \"typescript\" in the config to a node_modules/typescript folder.")
        payload = {"typescript": ts_mod, "httpClients": app.cfg.get("httpClients", ["api"]),
                   "files": [{"abs": os.path.join(app.root, r), "rel": r} for r in rels]}
        proc = subprocess.run(["node", script], input=json.dumps(payload), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ts_extract failed for {app.id}: {proc.stderr[-2000:]}")
        return json.loads(proc.stdout)

    # ---------- linking ----------

    def build(self):
        t0 = time.time()
        g = _Linker(self).link()
        self.version += 1
        g["version"] = self.version
        g["generated"] = time.time()
        g["name"] = self.cfg.get("name", "Project")
        self.graph = g
        self.timings["link"] = round(time.time() - t0, 3)
        g["timings"] = dict(self.timings)
        return g


class _Linker:
    def __init__(self, project):
        self.p = project
        self.files, self.fns, self.edges = [], [], []
        self.nodes = {}   # non-file, non-function nodes by id
        self.fn_ids = set()
        self.stats = defaultdict(int)
        self.unresolved = defaultdict(int)
        self.unmatched_http = []
        self._edge_seen = set()

    # -- helpers --
    def edge(self, s, t, k, **kw):
        key = (s, t, k, kw.get("l"))
        if key in self._edge_seen or s == t:
            return
        self._edge_seen.add(key)
        e = {"s": s, "t": t, "k": k}
        e.update({k2: v for k2, v in kw.items() if v is not None})
        self.edges.append(e)

    def node(self, nid, **kw):
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, **kw}
        return nid

    def ext_node(self, name):
        return self.node("ext:" + name, kind="ext", label=name)

    def trig(self, app, rel, line, label, kind, target, n=0):
        tid = f"tg:{app.id}:{rel}:{line}:{n}"
        self.node(tid, kind="trigger", label=label, tkind=kind, app=app.id, file=f"f:{app.id}:{rel}", line=line)
        self.edge(tid, target, "trig")

    @staticmethod
    def fid(app, rel, qual):
        return f"fn:{app.id}:{rel}#{qual}"

    def role(self, app, rel):
        r = rel.replace(os.sep, "/").lower()
        overrides = app.cfg.get("roles", {})
        for prefix, role in overrides.items():
            if r.startswith(prefix.lower()):
                return role
        if app.lang == "py":
            if "/routers/" in r or "/endpoints/" in r or r.endswith("main.py") or "/api/" in r and r.endswith("api.py"):
                return "route"
            if "/models/" in r or "/schemas/" in r or r.endswith("database.py") or "/db/" in r:
                return "data"
            return "logic"
        if r.endswith((".tsx", ".jsx")) and ("/pages/" in r or "/screens/" in r or "/app/" in r or r.count("/") == 0):
            return "screen"
        if "/components/" in r or "/ui/" in r or r.endswith((".tsx", ".jsx")) and "/navigation/" not in r and "/context/" not in r:
            return "ui"
        return "client"

    # -- main --
    def link(self):
        for app in self.p.apps:
            for rel, x in sorted(app.extract.items()):
                fns = x.get("functions", [])
                self.files.append({
                    "id": f"f:{app.id}:{rel}", "app": app.id, "rel": rel.replace(os.sep, "/"),
                    "label": os.path.basename(rel), "role": self.role(app, rel), "lines": x.get("lines", 0),
                    "error": x.get("error"),
                })
                for f in fns:
                    fid = self.fid(app, rel, f["name"])
                    if fid in self.fn_ids:
                        continue
                    self.fn_ids.add(fid)
                    kind = "component" if f.get("component") else ("method" if f.get("cls") else "fn")
                    rec = {"id": fid, "file": f"f:{app.id}:{rel}", "name": f["name"], "line": f["line"], "end": f["end"],
                           "hash": f["hash"], "kind": kind}
                    if f.get("async"):
                        rec["async"] = True
                    self.fns.append(rec)

        self.routes = {}  # app_id -> list of (method, segments, route_id, handler fid)
        for app in self.p.apps:
            if app.lang == "py":
                self.link_py(app)
        for app in self.p.apps:
            if app.lang != "py":
                self.link_ts(app)

        self.stats["files"] = len(self.files)
        self.stats["functions"] = len(self.fns)
        self.stats["edges"] = len(self.edges)
        return {
            "apps": [{"id": a.id, "name": a.name, "lang": a.lang, "root": a.root, "kind": "client" if a.lang != "py" else "backend",
                      "backend": a.cfg.get("backend")} for a in self.p.apps],
            "lanes": [{"id": k, "label": v} for k, v in LANES],
            "files": self.files, "fns": self.fns, "nodes": list(self.nodes.values()), "edges": self.edges,
            "stats": dict(self.stats),
            "unresolved": sorted(self.unresolved.items(), key=lambda kv: -kv[1])[:60],
            "unmatched_http": self.unmatched_http[:300],
        }

    # ================= Python =================

    def link_py(self, app):
        mods = {}       # module -> rel
        defs = {}       # module -> set(qual)
        for rel, x in app.extract.items():
            mods[x["module"]] = rel
            defs[x["module"]] = {f["name"] for f in x["functions"]}
        model_tables = {}
        classes = {}
        for x in app.extract.values():
            model_tables.update(x.get("model_tables", {}))
            classes[x["module"]] = set(x.get("classes", []))
        db = app.cfg.get("db") or {}
        db_id = db.get("id", app.id)

        def split_module(dotted):
            """'app.services.pricing.compute' -> ('app.services.pricing', 'compute')"""
            parts = dotted.split(".")
            for i in range(len(parts), 0, -1):
                m = ".".join(parts[:i])
                if m in mods:
                    return m, ".".join(parts[i:])
            return None, dotted

        def resolve(x, qual, target):
            """Return a function id, ('ext', name) or None."""
            module = x["module"]
            cls = None
            for f in x["functions"]:
                if f["name"] == qual:
                    cls = f.get("cls")
                    break
            parts = target.split(".")
            head = parts[0]
            local = defs.get(module, set())
            if head in ("self", "cls") and cls and len(parts) >= 2:
                q = cls + "." + parts[1]
                if q in local:
                    return self.fid(app, mods[module], q)
                return None
            if len(parts) == 1:
                # nested function in the caller or any enclosing scope, then module level
                scope = qual.split(".")
                for i in range(len(scope), 0, -1):
                    q = ".".join(scope[:i]) + "." + target
                    if q in local:
                        return self.fid(app, mods[module], q)
            if target in local:
                return self.fid(app, mods[module], target)
            if head in local or (head + ".__init__") in local:
                q = target if target in local else head + ".__init__"
                if q in local:
                    return self.fid(app, mods[module], q)
            if head in x["imports"]:
                full = x["imports"][head] + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
                m, rest = split_module(full)
                if m:
                    if not rest:
                        return None
                    d = defs.get(m, set())
                    if rest in d:
                        return self.fid(app, mods[m], rest)
                    if rest + ".__init__" in d:
                        return self.fid(app, mods[m], rest + ".__init__")
                    if rest.split(".")[0] in classes.get(m, ()):
                        return False  # data class / model constructor or attribute: not a function call
                    # Class.method via instance attribute chains we can't follow
                    return None
                top = full.split(".")[0]
                for key in (".".join(full.split(".")[:2]), top):
                    if key in self.p.ext_py:
                        return ("ext", self.p.ext_py[key]) if self.p.ext_py[key] else None
                return None
            return None

        # Functions
        for rel, x in app.extract.items():
            for c in x.get("calls", []):
                if c["from"] is None:
                    continue
                src = self.fid(app, rel, c["from"])
                r = resolve(x, c["from"], c["target"])
                if r is False:
                    continue
                if isinstance(r, tuple):
                    self.edge(src, self.ext_node(r[1]), "ext", line=c["line"])
                elif r:
                    self.edge(src, r, "call", line=c["line"], l=c.get("via"))
                else:
                    head = c["target"].split(".")[0]
                    if head in x["imports"] and split_module(x["imports"][head])[0]:
                        self.unresolved[f"{app.id}: {c['target']}"] += 1
                        self.stats["unresolved_calls"] += 1

            # DB tables (Supabase style)
            seen = {}
            for t in x.get("tables", []):
                key = (t["fn"], t["table"])
                prev = seen.get(key)
                if prev is None or prev == "access":
                    seen[key] = t["op"]
            for (fn, table), op in seen.items():
                kind = "rpc" if table.startswith("rpc:") else "table"
                name = table[4:] if kind == "rpc" else table
                tid = self.node(f"tb:{db_id}:{table}", kind="table", label=(name + "()") if kind == "rpc" else name,
                                db=db_id, dbname=db.get("name", "Database"), rpc=kind == "rpc")
                k = {"read": "read", "write": "write"}.get(op, "db")
                self.edge(self.fid(app, rel, fn), tid, k)
                if db.get("client"):
                    self.edge(self.fid(app, rel, fn), self.node(f"res:{app.id}:dbclient", kind="resource", label=db["client"],
                                                               app=app.id, rkind="dbclient", desc="One client shared by every request and thread"), "res")
            # DB tables (SQLAlchemy models)
            for ref in x.get("name_refs", []):
                tname = model_tables.get(ref["name"])
                if tname:
                    tid = self.node(f"tb:{db_id}:{tname}", kind="table", label=tname, db=db_id, dbname=db.get("name", "Database"))
                    self.edge(self.fid(app, rel, ref["fn"]), tid, "db", l=ref["name"])
                    if db.get("client"):
                        self.edge(self.fid(app, rel, ref["fn"]), self.node(f"res:{app.id}:dbclient", kind="resource", label=db["client"],
                                                                          app=app.id, rkind="dbclient", desc="Database sessions from one connection pool"), "res")

            # Shared thread pools
            for r in x.get("resources", []):
                rid = self._threadpool(app, r["res"])
                self.edge(self.fid(app, rel, r["fn"]), rid, "res", line=r["line"])

            # Outbound HTTP to known hosts
            for h in x.get("http_out", []):
                name = next((v for k, v in self.p.hosts.items() if k in h["host"]), h["host"])
                self.edge(self.fid(app, rel, h["fn"]), self.ext_node(name), "ext", line=h["line"], l=h["url"][:80])

            # Entry points: app startup, scheduled jobs, background tasks
            for i, s in enumerate(x.get("spawns", [])):
                target = resolve(x, s["from"], s["target"]) if s["from"] else self.fid(app, rel, s["target"])
                if isinstance(target, str) and target in self.fn_ids:
                    self.trig(app, rel, s["line"], s["kind"].capitalize(), s["kind"], target, i)

        # Routes: work out each router's mount prefix
        routers = {}   # (module, var) -> own prefix
        includes = []  # (parent key, child key, prefix)
        app_vars = []
        for rel, x in app.extract.items():
            m = x["module"]
            for var, pref in x.get("routers", {}).items():
                routers[(m, var)] = pref
            for v in x.get("app_vars", []):
                app_vars.append((m, v))
            for inc in x.get("includes", []):
                def key_of(dotted):
                    parts = dotted.split(".")
                    if len(parts) == 1:
                        if parts[0] in x["imports"]:
                            mm, rest = split_module(x["imports"][parts[0]])
                            return (mm, rest) if mm else None
                        return (m, parts[0])
                    head = parts[0]
                    if head in x["imports"]:
                        mm, rest = split_module(x["imports"][head] + "." + ".".join(parts[1:]))
                        return (mm, rest) if mm else None
                    return None
                pk, ck = key_of(inc["parent"]), key_of(inc["child"])
                if pk and ck:
                    includes.append((pk, ck, inc["prefix"] or ""))
        mount = {}

        def walk(key, acc, depth=0):
            if depth > 8:
                return
            own = routers.get(key, "")
            full = acc + own
            if key not in mount:
                mount[key] = full
            for pk, ck, pref in includes:
                if pk == key:
                    walk(ck, full + pref, depth + 1)
        for av in app_vars:
            walk(av, "")
        routes = []
        for rel, x in app.extract.items():
            m = x["module"]
            for r in x.get("routes", []):
                key = (m, r["owner"])
                prefix = mount.get(key, routers.get(key, ""))
                path = norm_path(prefix + r["path"])
                rid = self.node(f"rt:{app.id}:{r['method']} {path}", kind="route", app=app.id, method=r["method"], path=path,
                                label=f"{r['method']} {path}", file=f"f:{app.id}:{rel}", mounted=key in mount)
                handler = self.fid(app, rel, r["fn"])
                self.nodes[rid]["fn"] = handler
                self.edge(rid, handler, "trig")
                if r.get("sync"):
                    self.edge(handler, self._threadpool(app, "threadpool"), "res", l="sync def endpoint")
                routes.append((r["method"], path.strip("/").split("/") if path != "/" else [], rid, handler))
                if "webhook" in path or "callback" in path:
                    self.nodes[rid]["webhook"] = True
        self.routes[app.id] = routes
        self.stats[f"routes_{app.id}"] = len(routes)

    def _threadpool(self, app, kind):
        if kind == "threadpool":
            return self.node(f"res:{app.id}:threadpool", kind="resource", label="Worker threads (40)", app=app.id, rkind="threadpool",
                             desc="FastAPI/AnyIO default pool: run_in_threadpool and every sync def endpoint share 40 threads")
        return self.node(f"res:{app.id}:default-threads", kind="resource", label="Python default thread pool", app=app.id,
                         rkind="threadpool", desc="asyncio.to_thread / run_in_executor: min(32, CPUs + 4) threads")

    # ================= TypeScript =================

    def link_ts(self, app):
        X = app.extract
        alias = app.cfg.get("alias", {"@/": "src/"})
        exts = ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js"]

        def resolve_spec(rel, spec):
            if spec.startswith("."):
                base = os.path.normpath(os.path.join(os.path.dirname(rel), spec))
            else:
                base = None
                for a, target in alias.items():
                    if spec.startswith(a):
                        base = os.path.normpath(target + spec[len(a):])
                        break
                if base is None:
                    return None
            for e in exts:
                cand = base + e
                if cand in X:
                    return cand
            return None

        fn_names = {rel: {f["name"] for f in x["functions"]} for rel, x in X.items()}

        def resolve_export(rel, name, depth=0):
            """Exported `name` of file -> (rel, qual) of a function, or (rel, None) if it is not a function."""
            if depth > 6 or rel not in X:
                return None
            x = X[rel]
            if name == "default":
                d = x.get("defaultExport")
                if not d:
                    return None
                name2 = d
                return (rel, name2) if name2 in fn_names[rel] else (rel, None)
            local = x["exports"].get(name)
            if local:
                return (rel, local) if local in fn_names[rel] else (rel, None)
            for rx in x.get("reexports", []):
                tgt = resolve_spec(rel, rx["spec"])
                if not tgt:
                    continue
                if rx["names"] is None:
                    r = resolve_export(tgt, name, depth + 1)
                    if r and r[1]:
                        return r
                else:
                    for n in rx["names"]:
                        if n["local"] == name:
                            return resolve_export(tgt, n["imported"], depth + 1)
            return None

        bindings = {}  # rel -> local -> (kind, spec, imported)
        for rel, x in X.items():
            b = {}
            for imp in x["imports"]:
                if imp["def"]:
                    b[imp["def"]] = ("default", imp["spec"], "default")
                if imp["ns"]:
                    b[imp["ns"]] = ("ns", imp["spec"], None)
                for n in imp["names"]:
                    b[n["local"]] = ("named", imp["spec"], n["imported"])
            bindings[rel] = b

        destructured = {}  # (rel, fn qual, local) -> (prop, hook)
        for rel, x in X.items():
            for d in x.get("destructured", []):
                destructured[(rel, d["from"], d["local"])] = (d["prop"], d["hook"])

        def resolve(rel, qual, target, allow_guess=True):
            """-> (fid, confidence) | ('ext', name) | None"""
            names = fn_names[rel]
            parts = target.split(".")
            head = parts[0]
            if head == "this" and len(parts) >= 2:
                cls = qual.split(".")[0]
                q = cls + "." + parts[1]
                return (self.fid(app, rel, q), None) if q in names else None
            if len(parts) == 1:
                scope = qual.split(".")
                for i in range(len(scope), 0, -1):
                    q = ".".join(scope[:i]) + "." + target
                    if q in names:
                        return (self.fid(app, rel, q), None)
                if target in names:
                    return (self.fid(app, rel, target), None)
            elif target in names:
                return (self.fid(app, rel, target), None)
            b = bindings[rel].get(head)
            if b:
                kind, spec, imported = b
                tgt = resolve_spec(rel, spec)
                if not tgt:
                    name = next((v for k, v in self.p.ext_ts.items() if spec == k or spec.startswith(k + "/")), None)
                    return ("ext", name) if name else None
                if kind == "ns":
                    if len(parts) < 2:
                        return None
                    r = resolve_export(tgt, parts[1])
                    return (self.fid(app, r[0], r[1]), None) if r and r[1] else None
                r = resolve_export(tgt, imported)
                if r and not r[1] and len(parts) > 1:
                    # exported object of functions or class instance: admin.getZones / orderService.getMyOrders
                    local = X[r[0]]["exports"].get(imported, imported) if imported != "default" else X[r[0]].get("defaultExport")
                    if local:
                        local = X[r[0]].get("instances", {}).get(local, local)
                        q = local + "." + ".".join(parts[1:])
                        if q in fn_names[r[0]]:
                            return (self.fid(app, r[0], q), None)
                if r and r[1]:
                    q = r[1] + ("." + ".".join(parts[1:]) if len(parts) > 1 else "")
                    if q in fn_names[r[0]]:
                        return (self.fid(app, r[0], q), None)
                    if len(parts) == 1:
                        return (self.fid(app, r[0], r[1]), None)
                if len(parts) > 1:
                    # default-exported object of functions: api.getX -> export getX in that module
                    r2 = resolve_export(tgt, parts[1])
                    if r2 and r2[1]:
                        return (self.fid(app, r2[0], r2[1]), None)
                    # default export is an instance of a class in that file: service.method
                    for n in fn_names.get(tgt, ()):
                        if n.endswith("." + parts[1]) and n.count(".") == 1:
                            return (self.fid(app, tgt, n), "guess")
                return None
            if allow_guess and len(parts) == 1:
                scope = qual.split(".")
                for i in range(len(scope), 0, -1):
                    d = destructured.get((rel, ".".join(scope[:i]), target))
                    if d:
                        prop, hook = d
                        h = resolve(rel, qual, hook, allow_guess=False)
                        if h and h[0] != "ext":
                            hook_rel = h[0].split(":", 2)[2].split("#")[0]
                            for n in sorted(fn_names.get(hook_rel, ())):
                                if n.endswith("." + prop) and n.count(".") == 1:
                                    return (self.fid(app, hook_rel, n), "guess")
                            return (h[0], "guess")
                        break
            return None

        # constants for URL building, app-wide
        consts = {}
        for x in X.values():
            consts.update(x.get("consts", {}))

        def url_of(parts, depth=0):
            if parts is None or depth > 4:
                return None
            out = []
            for i, p in enumerate(parts):
                if "s" in p:
                    out.append(p["s"])
                elif "p" in p:
                    out.append("{}")
                elif "ref" in p:
                    v = consts.get(p["ref"])
                    s = url_of(v, depth + 1) if v else None
                    if s is None or s.startswith("http") or s == "{}":
                        if not "".join(out):
                            continue  # base URL variable at the start: drop it
                        out.append("{}")
                    else:
                        out.append(s)
            return "".join(out)

        prefix = app.cfg.get("pathPrefix", "")
        backends = app.cfg.get("backend")
        backends = [backends] if isinstance(backends, str) else (backends or [])

        def match_route(method, path):
            segs = path.strip("/").split("/") if path != "/" else []
            best, best_score = None, -1
            for be in backends:
                for (m, rsegs, rid, handler) in self.routes.get(be, []):
                    if m != method or len(rsegs) != len(segs):
                        continue
                    score = 0
                    ok = True
                    for a, b in zip(segs, rsegs):
                        if a == b:
                            score += 2
                        elif _seg_is_param(a) or _seg_is_param(b):
                            score += 0
                        else:
                            ok = False
                            break
                    if ok and score > best_score:
                        best, best_score = (rid, handler), score
            return best

        screen_dirs = tuple(app.cfg.get("screenDirs", ["/pages/", "/screens/"]))
        for rel, x in X.items():
            names = fn_names[rel]
            for c in x.get("calls", []):
                r = resolve(rel, c["from"], c["target"])
                src = self.fid(app, rel, c["from"])
                if r is None:
                    b = bindings[rel].get(c["target"].split(".")[0])
                    if not c.get("ref") and b and resolve_spec(rel, b[1]):
                        self.unresolved[f"{app.id}: {c['target']}"] += 1
                        self.stats["unresolved_calls"] += 1
                    continue
                if r[0] == "ext":
                    self.edge(src, self.ext_node(r[1]), "ext", line=c["line"])
                else:
                    self.edge(src, r[0], "call", line=c["line"], c=r[1])
            db = app.cfg.get("db")
            for t in x.get("tables", []):
                db_id = (db or {}).get("id", "supabase")
                table = t["table"]
                rpc = table.startswith("rpc:")
                label = (table[4:] + "()") if rpc else (table[8:] + " (storage)") if table.startswith("storage:") else table
                tid = self.node(f"tb:{db_id}:{table}", kind="table", label=label,
                                db=db_id, dbname=(db or {}).get("name", "Supabase Postgres"), rpc=rpc)
                self.edge(self.fid(app, rel, t["fn"]), tid, {"read": "read", "write": "write"}.get(t["op"], "db"),
                          line=t["line"], l="direct from client")
            for rd in x.get("renders", []):
                r = resolve(rel, rd["from"], rd["tag"])
                if r and r[0] != "ext":
                    self.edge(self.fid(app, rel, rd["from"]), r[0], "renders", line=rd["line"], c=r[1])
            for i, t in enumerate(x.get("triggers", [])):
                r = resolve(rel, t["from"], t["target"])
                if r and r[0] != "ext":
                    label = f"{t['event']} on <{t['tag']}>" + (f" “{t['text']}”" if t.get("text") else "")
                    self.trig(app, rel, t["line"], label, "ui", r[0], i)
            for h in x.get("http", []):
                path = url_of(h.get("url"))
                src = self.fid(app, rel, h["from"])
                if path is None:
                    self.stats["http_unknown_url"] += 1
                    continue
                path = norm_path(prefix + path) if not path.startswith("http") else path
                if path.startswith("http"):
                    host = path.split("/")[2] if path.count("/") >= 2 else path
                    name = next((v for k, v in self.p.hosts.items() if k in host), host)
                    self.edge(src, self.ext_node(name), "ext", line=h["line"], l=path[:80])
                    continue
                m = match_route(h["method"], path)
                label = f"{h['method']} {path}"
                if m:
                    self.edge(src, m[1], "http", line=h["line"], l=label)
                    self.stats["http_matched"] += 1
                else:
                    be = backends[0] if backends else "?"
                    rid = self.node(f"rt:{be}:{h['method']} {path}", kind="route", app=be, method=h["method"], path=path,
                                    label=label, missing=True)
                    self.edge(src, rid, "http", line=h["line"], l=label)
                    self.stats["http_unmatched"] += 1
                    self.unmatched_http.append({"app": app.id, "file": rel, "line": h["line"], "call": label})
            # Screens: top-level components in pages/ or screens/ start when the screen opens
            if any(d in "/" + rel.replace(os.sep, "/") for d in screen_dirs):
                for f in x["functions"]:
                    if f.get("component") and not f.get("parent"):
                        self.trig(app, rel, f["line"], "Screen opens", "screen", self.fid(app, rel, f["name"]), 900)
            for f in x["functions"]:
                if f["name"] in ("App", "default") and not f.get("parent") and rel.count(os.sep) <= 1 and "App" in rel:
                    self.trig(app, rel, f["line"], "App starts", "app", self.fid(app, rel, f["name"]), 901)
            _ = names
