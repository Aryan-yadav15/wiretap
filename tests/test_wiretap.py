"""End-to-end tests on a small fixture project: indexing, linking, and a simulated Claude Code session.

Run:  python3 -m unittest discover -s tests -v
TypeScript is found in a node_modules folder; set WIRETAP_TYPESCRIPT=/path/to/node_modules/typescript if needed.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from wiretap.graph import Project  # noqa: E402
from wiretap.server import GraphIndex, State, make_handler, term_regex  # noqa: E402
from wiretap.session import parse_tests  # noqa: E402
from wiretap.simplify import DescribeError, Describer, cut_steps, jobs_for  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


def find_typescript():
    if os.environ.get("WIRETAP_TYPESCRIPT"):
        return
    for cand in [os.path.join(HERE, "..", "node_modules", "typescript")]:
        if os.path.isdir(cand):
            os.environ["WIRETAP_TYPESCRIPT"] = os.path.abspath(cand)


find_typescript()


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiretap-test-")
        self.root = os.path.join(self.tmp, "fixture")
        shutil.copytree(os.path.join(HERE, "fixture"), self.root)
        self.config = os.path.join(self.root, "wiretap.config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self):
        p = Project(self.config)
        p.scan()
        return p, p.build()


class TestGraph(FixtureCase):
    def test_routes_http_and_triggers(self):
        p, g = self.build()
        routes = {n["label"] for n in g["nodes"] if n["kind"] == "route" and not n.get("missing")}
        self.assertIn("POST /api/orders", routes)
        self.assertIn("GET /api/orders/{order_id}", routes)

        http = [e for e in g["edges"] if e["k"] == "http"]
        create = next(e for e in http if e["s"].endswith("#createOrder"))
        self.assertEqual(create["t"], "fn:api:app/routers/orders.py#create_order")
        missing = [n for n in g["nodes"] if n.get("missing")]
        self.assertEqual([n["label"] for n in missing], ["GET /api/nothing-here"])

        # button -> handler -> api function
        trig = [n for n in g["nodes"] if n["kind"] == "trigger" and "Place Order" in n["label"]]
        self.assertEqual(len(trig), 1)
        handler = next(e["t"] for e in g["edges"] if e["s"] == trig[0]["id"])
        self.assertTrue(handler.endswith("#Checkout.handlePlace"))
        self.assertTrue(any(e["s"] == handler and e["t"].endswith("#createOrder") for e in g["edges"]))

    def test_python_calls_tables_and_resources(self):
        p, g = self.build()
        E = {(e["s"], e["t"], e["k"]) for e in g["edges"]}
        co = "fn:api:app/routers/orders.py#create_order"
        self.assertIn((co, "fn:api:app/services/pricing.py#compute_total", "call"), E)
        self.assertIn(("fn:api:app/services/pricing.py#compute_total", "fn:api:app/services/pricing.py#pay_later_fee", "call"), E)
        self.assertIn((co, "tb:pg:orders", "write"), E)
        self.assertIn(("fn:api:app/services/pricing.py#pay_later_fee", "tb:pg:charges", "read"), E)
        self.assertIn((co, "res:api:threadpool", "res"), E)
        # sync def endpoint runs on the thread pool too
        self.assertIn(("fn:api:app/routers/orders.py#get_order", "res:api:threadpool", "res"), E)
        starts = [n for n in g["nodes"] if n["kind"] == "trigger" and n.get("tkind") == "app startup"]
        self.assertEqual(len(starts), 1)

    def test_cache_round_trip(self):
        p, g = self.build()
        p.save_cache()
        p2 = Project(self.config)
        p2.load_cache()
        self.assertEqual(p2.scan(), [])
        self.assertEqual(p2.build()["stats"]["edges"], g["stats"]["edges"])


class TestStacks(unittest.TestCase):
    """The adapters beyond FastAPI + Supabase: Express (CommonJS, mounted routers), Next.js and Expo-style
    file routes, Flask blueprints, Prisma, and the coverage report. Fixture: tests/fixture-stacks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wiretap-stacks-")
        self.root = os.path.join(self.tmp, "stacks")
        shutil.copytree(os.path.join(HERE, "fixture-stacks"), self.root)
        self.config = os.path.join(self.root, "wiretap.config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, edit=None):
        if edit:
            with open(self.config) as fh:
                cfg = json.load(fh)
            edit(cfg)
            with open(self.config, "w") as fh:
                json.dump(cfg, fh)
        p = Project(self.config)
        p.scan()
        return p.build()

    def test_routes_from_every_adapter(self):
        g = self.build()
        routes = {n["id"]: n.get("fn") for n in g["nodes"] if n["kind"] == "route" and not n.get("missing")}
        self.assertEqual(routes, {
            # Express: router mounted with app.use('/api/orders', require(...)), a handler from another file, inline ones
            "rt:express:GET /api/orders/{id}": "fn:express:src/controllers/orders.js#getOrder",
            "rt:express:POST /api/orders": "fn:express:src/routes/orders.js#POST /",
            "rt:express:GET /health": "fn:express:src/server.js#GET /health",
            # Next.js: app router route.ts and a pages/api default export
            "rt:next:GET /api/items/{id}": "fn:next:app/api/items/[id]/route.ts#GET",
            "rt:next:DELETE /api/items/{id}": "fn:next:app/api/items/[id]/route.ts#DELETE",
            "rt:next:ANY /api/health": "fn:next:pages/api/health.ts#handler",
            # Flask: a blueprint with url_prefix, registered under /v1 inside create_app()
            "rt:flask:GET /v1/orders/{order_id}": "fn:flask:app/views.py#order",
            "rt:flask:DELETE /v1/orders/{order_id}": "fn:flask:app/views.py#order",
            "rt:flask:GET /v1/orders/recent": "fn:flask:app/views.py#recent",
        })
        # an app with routes and no UI is a backend; Next.js (both) stays with the apps
        self.assertEqual({a["id"]: a["kind"] for a in g["apps"]},
                         {"express": "backend", "next": "client", "web": "client", "admin": "client", "flask": "backend"})
        # Flask's sync views don't take FastAPI's worker threads
        self.assertFalse([e for e in g["edges"] if e["k"] == "res" and e["s"].startswith("fn:flask:")])

    def test_http_calls_reach_typescript_backends(self):
        g = self.build()
        http = {(e["s"], e["t"]) for e in g["edges"] if e["k"] == "http"}
        self.assertIn(("fn:web:src/orders.ts#loadOrder", "fn:express:src/controllers/orders.js#getOrder"), http)
        self.assertIn(("fn:web:src/orders.ts#placeOrder", "fn:express:src/routes/orders.js#POST /"), http)
        # a Next.js page's fetch reaches its own app's routes, any-method ones too
        self.assertIn(("fn:next:app/items/page.tsx#ItemsPage.remove", "fn:next:app/api/items/[id]/route.ts#DELETE"), http)
        self.assertIn(("fn:next:app/items/page.tsx#ItemsPage.check", "fn:next:pages/api/health.ts#handler"), http)
        self.assertEqual(g["stats"].get("http_unmatched", 0), 0)

    def test_client_with_no_backend_set_uses_apps_with_routes(self):
        def drop_backend(cfg):
            for a in cfg["apps"]:
                a.pop("backend", None)
        g = self.build(drop_backend)
        http = {(e["s"], e["t"]) for e in g["edges"] if e["k"] == "http"}
        self.assertIn(("fn:web:src/orders.ts#loadOrder", "fn:express:src/controllers/orders.js#getOrder"), http)

    def test_missing_path_prefix_is_worked_out(self):
        g = self.build()
        http = {(e["s"], e["t"]) for e in g["edges"] if e["k"] == "http"}
        # admin calls /orders/{id}; Flask serves /v1/orders/{order_id}: with no pathPrefix set, /v1 is added
        self.assertIn(("fn:admin:src/orders.ts#getOrder", "fn:flask:app/views.py#order"), http)
        self.assertIn(("fn:admin:src/orders.ts#recentOrders", "fn:flask:app/views.py#recent"), http)
        admin = next(a for a in g["coverage"]["apps"] if a["id"] == "admin")
        self.assertEqual((admin["guessed_prefix"], admin["http"]["matched"]), ("/v1", 3))
        # a config that sets pathPrefix (even to "") is taken as it is
        def no_guess(cfg):
            next(a for a in cfg["apps"] if a["id"] == "admin")["pathPrefix"] = ""
        g = self.build(no_guess)
        self.assertEqual(next(a for a in g["coverage"]["apps"] if a["id"] == "admin")["http"]["matched"], 0)

    def test_prisma_tables_use_schema_names(self):
        g = self.build()
        E = {(e["s"], e["t"], e["k"]) for e in g["edges"]}
        self.assertIn(("fn:express:src/controllers/orders.js#getOrder", "tb:prisma:orders", "read"), E)
        self.assertIn(("fn:express:src/routes/orders.js#POST /", "tb:prisma:order_items", "write"), E)
        self.assertIn(("fn:next:app/api/items/[id]/route.ts#DELETE", "tb:prisma:item", "write"), E)  # no schema: the model name

    def test_coverage_names_what_it_read_and_what_it_did_not(self):
        cov = self.build()["coverage"]
        apps = {a["id"]: a for a in cov["apps"]}
        self.assertEqual(apps["express"]["found"], {"routes": {"express": 3}, "tables": {"prisma": 3}})
        self.assertEqual(apps["flask"]["found"]["routes"], {"flask": 3})
        self.assertEqual(apps["web"]["http"], {"calls": 2, "matched": 2, "unmatched": 0, "unknown_url": 0})
        self.assertEqual(cov["not_parsed"], [{"lang": "Go", "files": 1}])
        self.assertEqual(cov["not_read"], [{"app": "next", "dep": "drizzle-orm", "what": "Drizzle queries"}])

    def test_commonjs_imports(self):
        g = self.build()
        self.assertIn("f:express:src/routes/orders.js", g["imports"]["f:express:src/server.js"])
        self.assertIn("f:express:src/controllers/orders.js", g["imports"]["f:express:src/routes/orders.js"])

    def test_pages_load_scripts_and_scripts_use_stylesheets(self):
        g = self.build()
        # <script src> and <link href> from the site root are read from the page's folder; other sites are skipped
        self.assertEqual(g["imports"]["f:web:src/index.html"], ["f:web:src/page.js", "f:web:src/style.css"])
        # two of the sheet's classes make a link; one ("active") doesn't
        self.assertEqual(g["styles"], {"f:web:src/page.js": ["f:web:src/style.css"]})
        gi = GraphIndex(g)
        links = gi.links(["f:web:src/index.html", "f:web:src/page.js", "f:web:src/style.css", "f:web:src/toggle.js"])
        self.assertEqual(sorted(links["f:web:src/page.js"]), [["f:web:src/index.html", "loaded by"], ["f:web:src/style.css", "styled by"]])
        self.assertEqual(links["f:web:src/toggle.js"], [])
        self.assertEqual(gi.stray(links), {"f:web:src/toggle.js"})


class TestHelpers(unittest.TestCase):
    def test_term_regex(self):
        rx = term_regex(["cod", "pay_later"])
        for s in ["cod", "COD", "isCod", "cod_fee", "pay_later_fee"]:
            self.assertTrue(rx.search(s), s)
        for s in ["code", "decode", "promo_code", "PROMO_CODE", "Codec"]:
            self.assertFalse(rx.search(s), s)

    def test_outbound_http_needs_a_url(self):
        from wiretap.py_extract import extract
        src = 'import requests\ndef f(x):\n    x.get("http_out", [])\n    return requests.get("https://api.stripe.com/v1/charges")\n'
        hosts = [h["host"] for h in extract(src, "a.py", "a").get("http_out", [])]
        self.assertEqual(hosts, ["api.stripe.com"])  # the dict lookup is not a web request

    def test_parse_tests(self):
        self.assertEqual(parse_tests("==== 1 failed, 212 passed in 3.2s ===="), {"passed": 212, "failed": 1})
        self.assertEqual(parse_tests("Tests:       2 failed, 40 passed, 42 total"), {"passed": 40, "failed": 2})
        self.assertEqual(parse_tests("....\n94 passed, 2 deselected in 0.99s"), {"passed": 94, "failed": 0})
        self.assertEqual(parse_tests("ℹ tests 3\nℹ pass 2\nℹ fail 1\nℹ cancelled 0"), {"passed": 2, "failed": 1})
        self.assertEqual(parse_tests("ℹ fail 0\nℹ cancelled 0\nℹ skipped 0"), {"passed": None, "failed": 0})
        self.assertIsNone(parse_tests("hello"))


class TestLiveSession(FixtureCase):
    def hook(self, state, **payload):
        payload.setdefault("session_id", "s1")
        payload.setdefault("cwd", self.root)
        state.on_hook(payload)

    def edit(self, state, rel, old, new):
        path = os.path.join(self.root, rel)
        with open(path) as fh:
            src = fh.read()
        self.assertIn(old, src)
        with open(path, "w") as fh:
            fh.write(src.replace(old, new))
        self.hook(state, hook_event_name="PostToolUse", tool_name="Edit",
                  tool_input={"file_path": path, "old_string": old, "new_string": new})

    def test_session_flow(self):
        state = State(self.config)
        pricing = os.path.join(self.root, "backend/app/services/pricing.py")
        self.hook(state, hook_event_name="UserPromptSubmit", prompt="Cap the pay later fee at 50")
        self.hook(state, hook_event_name="PostToolUse", tool_name="Read", tool_input={"file_path": pricing})
        self.hook(state, hook_event_name="PostToolUse", tool_name="ExitPlanMode",
                  tool_input={"plan": "1. Change `app/services/pricing.py` pay_later_fee to cap at 50.\n2. Add tests in tests/test_cap.py"})
        self.edit(state, "backend/app/services/pricing.py", 'return row.data[0]["amount"]', 'return min(50, row.data[0]["amount"])')
        self.edit(state, "backend/app/routers/orders.py", 'return row\n', 'return {"row": row, "total": total}\n')
        self.hook(state, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "pytest -q"},
                  tool_response={"stdout": "==== 1 failed, 9 passed in 0.4s ====", "stderr": ""})
        self.hook(state, hook_event_name="Stop")

        snap = state.sessions.snapshot(state.sessions.by_id["s1"], state.index)
        t = snap["tasks"][-1]
        kinds = [e["k"] for e in t["events"]]
        self.assertEqual(kinds, ["prompt", "read", "plan", "edit", "edit", "test", "stop"])
        pf, of = "f:api:app/services/pricing.py", "f:api:app/routers/orders.py"
        self.assertEqual(t["planned"], [pf])
        self.assertEqual(t["planned_new"], ["tests/test_cap.py"])
        self.assertEqual(t["touched"][pf]["fns"], {"pay_later_fee": "changed"})
        self.assertEqual(t["touched"][of]["fns"], {"create_order": "changed"})
        self.assertEqual(t["summary"]["off_plan"], [of])
        self.assertEqual(t["summary"]["tests"]["failed"], 1)
        # the web app calls create_order over HTTP, so it is in the ripple
        self.assertIn("f:web:src/services/api.ts", t["ripple_files"])
        self.assertEqual(t["events"][4].get("off"), True)

    def test_new_file_and_editor_changes(self):
        state = State(self.config)
        self.hook(state, hook_event_name="UserPromptSubmit", prompt="Add refunds")
        path = os.path.join(self.root, "backend/app/services/refunds.py")
        with open(path, "w") as fh:
            fh.write("def refund(order_id):\n    return order_id\n")
        self.hook(state, hook_event_name="PostToolUse", tool_name="Write", tool_input={"file_path": path, "content": "x\ny\n"})
        t = state.sessions.snapshot(state.sessions.by_id["s1"], state.index)["tasks"][-1]
        self.assertEqual(t["touched"]["f:api:app/services/refunds.py"]["status"], "created")
        self.assertTrue(any(f["id"] == "f:api:app/services/refunds.py" for f in state.graph["files"]))

        # an edit nobody announced (the user's editor) is picked up by the watcher
        time.sleep(0.01)
        with open(os.path.join(self.root, "backend/app/database.py"), "a") as fh:
            fh.write("\n\ndef close_db():\n    pass\n")
        changes = state.project.scan()
        for app_id, rel, old, new in changes:
            state.sessions.on_file_change(state.project.app_by_id[app_id], rel, old, new)
        ed = state.sessions.by_id["editor"].tasks[-1]
        self.assertIn("f:api:app/database.py", ed.touched)
        self.assertEqual(ed.touched["f:api:app/database.py"]["fns"], {"close_db": "added"})

    def test_handback_transcript_prompt_and_shell_kinds(self):
        state = State(self.config)
        transcript = os.path.join(self.tmp, "t.jsonl")
        with open(transcript, "w") as fh:
            for msg in [{"type": "user", "message": {"role": "user", "content": "Cap the pay later fee"}},
                        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
                        {"type": "user", "message": {"role": "user", "content": "<agent-message from=\"a1\">report</agent-message>"}}]:
                fh.write(json.dumps(msg) + "\n")
        pricing = os.path.join(self.root, "backend/app/services/pricing.py")
        # Wiretap missed the prompt, so the first tool call reads it from the transcript
        self.hook(state, hook_event_name="PostToolUse", tool_name="Read", tool_input={"file_path": pricing}, transcript_path=transcript)
        self.hook(state, hook_event_name="Stop")
        self.hook(state, hook_event_name="UserPromptSubmit",
                  prompt='Another Claude session sent a message:\n<agent-message from="a1">\n[Subagent hand-back] Done: fee capped.\n</agent-message>')
        # writing a test file is not running the tests; pytest -q's summary is read; a commit is not a search
        self.hook(state, hook_event_name="PostToolUse", tool_name="Bash",
                  tool_input={"command": "cat > tests/test_cap.py <<'EOF'\nimport pytest\nEOF"}, tool_response={"stdout": ""})
        self.hook(state, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "uv run pytest -q 2>&1 | tail -1"},
                  tool_response={"stdout": "94 passed, 2 deselected in 0.99s"})
        self.hook(state, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "git add -A && git commit -q -m cap"},
                  tool_response={"stdout": " app/services/pricing.py | 2 +-"})
        tasks = state.sessions.snapshot(state.sessions.by_id["s1"], state.index)["tasks"]
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["prompt"], "Cap the pay later fee")
        self.assertIsNone(t["ended"])
        kinds = [e["k"] for e in t["events"]]
        self.assertEqual(kinds, ["read", "stop", "handback", "run", "test", "run"])
        self.assertEqual(t["events"][2]["text"], "Done: fee capped.")
        self.assertEqual(t["tests"]["passed"], 94)

    def test_stray_edit_and_imports(self):
        state = State(self.config)
        imports = state.graph["imports"]
        self.assertIn("f:web:src/services/api.ts", imports["f:web:src/pages/Checkout.tsx"])
        self.hook(state, hook_event_name="UserPromptSubmit", prompt="Cap the fee")
        self.edit(state, "backend/app/services/pricing.py", 'return row.data[0]["amount"]', 'return min(50, row.data[0]["amount"])')
        self.edit(state, "backend/app/routers/orders.py", 'return row\n', 'return {"row": row, "total": total}\n')
        path = os.path.join(self.root, "backend/app/services/banner.py")
        with open(path, "w") as fh:
            fh.write("def banner():\n    return 'hi'\n")
        self.hook(state, hook_event_name="PostToolUse", tool_name="Write", tool_input={"file_path": path, "content": "x\n"})
        pkg = os.path.join(self.root, "backend/app/services/extra/__init__.py")
        os.makedirs(os.path.dirname(pkg))
        open(pkg, "w").close()
        self.hook(state, hook_event_name="PostToolUse", tool_name="Write", tool_input={"file_path": pkg, "content": ""})
        t = state.sessions.snapshot(state.sessions.by_id["s1"], state.index)["tasks"][-1]
        # pricing and orders are linked by a call; the new banner file links to nothing else that changed,
        # and an empty package marker is never flagged
        self.assertEqual(t["summary"]["stray"], ["f:api:app/services/banner.py"])


class TestProjects(FixtureCase):
    """A Claude session in a folder with no config gets that folder mapped on its own."""

    def test_session_in_another_folder(self):
        other = os.path.join(self.tmp, "wt-other-proj")
        os.makedirs(os.path.join(other, "src"))
        with open(os.path.join(other, "package.json"), "w") as fh:
            json.dump({"name": "other-app"}, fh)
        with open(os.path.join(other, "src", "main.ts"), "w") as fh:
            fh.write("export function greet(name: string) {\n  return 'hi ' + name;\n}\n")
        state = State(self.config)
        try:
            state.on_hook({"session_id": "o1", "cwd": other, "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
            state.on_hook({"session_id": "o1", "cwd": other, "hook_event_name": "PostToolUse", "tool_name": "Read",
                           "tool_input": {"file_path": os.path.join(other, "src", "main.ts")}})
            ps = state.projects["wt-other-proj"]
            self.assertTrue(ps.auto)
            self.assertEqual([a["name"] for a in ps.graph["apps"]], ["other-app"])
            ev = ps.snapshot(ps.sessions.by_id["o1"])["tasks"][-1]["events"]
            self.assertEqual(ev[-1]["file"], "f:wt-other-proj:src/main.ts")
            self.assertNotIn("o1", state.sessions.by_id)  # the config's project is untouched
            # a session inside the configured project still lands there
            state.on_hook({"session_id": "c1", "cwd": os.path.join(self.root, "backend"), "hook_event_name": "UserPromptSubmit", "prompt": "x"})
            self.assertIn("c1", state.sessions.by_id)
        finally:
            shutil.rmtree(state.projects["wt-other-proj"].project.cache_dir, ignore_errors=True)


def listed(prompt):
    """Function names a describe prompt asks for ("- name: 10-24")."""
    return re.findall(r"^- (\S+): \d+-\d+$", prompt, re.M)


class FakeModel:
    """Stands in for claude -p: names every listed function, plus one the parser never found."""

    def __init__(self, steps=None):
        self.calls, self.steps = [], steps

    def __call__(self, prompt, stdin):
        self.calls.append(prompt)
        if prompt.startswith("Below are the steps"):
            n = int(re.search(r"exactly (\d+) names", prompt).group(1)) if self.steps is None else self.steps
            return json.dumps({"title": "Customer places an order", "steps": [f"Step {i + 1}" for i in range(n)]})
        fns = {n: {"name": f"Does {n}", "line": f"Explains {n}."} for n in listed(prompt)}
        fns["made_up_function"] = {"name": "Invented", "line": "Not in the file."}
        return "Sure, here it is:\n" + json.dumps({"file": {"name": "Order pricing", "line": "Works out totals."}, "functions": fns})


class TestSimplify(FixtureCase):
    """Simple mode (ADR 0006): the model names what the parser found and nothing else; names are cached by hash."""

    def test_describe_file_drops_invented_names_and_caches_by_hash(self):
        p, g = self.build()
        model = FakeModel()
        d = Describer(p.cache_dir, run=model)
        fid = next(f["id"] for f in g["files"] if f["rel"].endswith("pricing.py"))
        def text_of(f):
            with open(os.path.join(self.root, "backend", f.split(":", 2)[2])) as fh:
                return fh.read().splitlines()
        jobs = jobs_for(g, [fid], text_of, {}, d)
        self.assertEqual(len(jobs), 1)
        res = d.describe_file(jobs[0])
        fns = [f for f in g["fns"] if f["file"] == fid]
        self.assertTrue(fns)
        self.assertEqual(set(res["fns"]), {f["id"] for f in fns})  # made_up_function is not on the map
        self.assertEqual(res["files"][fid]["n"], "Order pricing")
        # fresh, and kept on disk
        self.assertEqual(jobs_for(g, [fid], text_of, {}, d), [])
        again = Describer(p.cache_dir, run=model).fresh(g)
        self.assertEqual(again["fns"][fns[0]["id"]]["n"], f"Does {fns[0]['name']}")
        # a changed function is stale again, and only its description stops being served
        g["fns"] = [{**f, "hash": "changed"} if f["id"] == fns[0]["id"] else f for f in g["fns"]]
        self.assertNotIn(fns[0]["id"], d.fresh(g)["fns"])
        jobs = jobs_for(g, [fid], text_of, {}, d)
        self.assertEqual(len(jobs), 1)
        # only the changed function is asked for again; the others keep their names
        d.describe_file(jobs[0])
        self.assertEqual(listed(model.calls[-1]), [fns[0]["name"]])
        self.assertEqual(len(d.fresh(g)["fns"]), len(fns))

    def test_a_file_with_many_functions_is_split_across_calls(self):
        model = FakeModel()
        d = Describer(self.tmp, run=model)
        fi = {"id": "f:a:big.py", "rel": "big.py", "label": "big.py", "lines": 900, "app": "a"}
        fns = [{"id": f"fn:a:big.py#f{i}", "name": f"f{i}", "line": i * 10, "end": i * 10 + 5, "hash": str(i), "file": fi["id"]} for i in range(45)]
        res = d.describe_file({"file": fi, "fns": fns, "lines": ["pass"] * 900, "where": "a · big.py"})
        self.assertEqual(len(model.calls), 3)  # 20 + 20 + 5
        self.assertEqual(len(res["fns"]), 45)

    def test_flow_steps_are_cut_by_layer_and_must_all_be_named(self):
        items = [{"id": "tg:1", "layer": "start"}, {"id": "fn:a", "layer": "screen"}, {"id": "fn:b", "layer": "client"},
                 {"id": "fn:c", "layer": "client"}, {"id": "rt:x", "layer": "route"}, {"id": "fn:d", "layer": "logic"}]
        steps = cut_steps(items)
        self.assertEqual([[i["id"] for i in st["items"]] for st in steps], [["tg:1"], ["fn:a"], ["fn:b", "fn:c"], ["rt:x"], ["fn:d"]])
        for st in steps:
            for it in st["items"]:
                it["label"] = it["id"]
        d = Describer(self.tmp, run=FakeModel(steps=3))
        with self.assertRaises(DescribeError):
            d.flow(steps)
        model = FakeModel()
        d = Describer(self.tmp, run=model)
        self.assertEqual(d.flow(steps)["steps"], ["Step 1", "Step 2", "Step 3", "Step 4", "Step 5"])
        d.flow(steps)
        self.assertEqual(len(model.calls), 1)  # cached

    def test_server_names_a_flow_and_describes_its_files(self):
        state = State(self.config)
        ps = state.primary
        model = FakeModel()
        ps.describer.run = model
        g = ps.graph
        route = next(n for n in g["nodes"] if n["kind"] == "route" and n["label"] == "POST /api/orders")
        handler = route["fn"]
        callee = next((e["t"] for e in g["edges"] if e["s"] == handler and e["k"] == "call" and e["t"].startswith("fn:")), None)
        ids = [route["id"]] + [i for i in (handler, callee) if i]
        res = ps.flow(ids)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["title"], "Customer places an order")
        self.assertEqual(sum(len(st["ids"]) for st in res["steps"]), len(ids))  # every function is in a step
        self.assertEqual(res["steps"][0]["layer"], "route")
        self.assertTrue(state.primary.describer.fresh(g)["fns"])  # the flow's files were described on the way


class TestAsk(unittest.TestCase):
    def test_answer_streams_from_a_read_only_claude(self):
        from wiretap import ask
        # a fake `claude` on PATH prints what `claude -p --output-format stream-json` would, and the arguments it got
        d = tempfile.mkdtemp(prefix="wt-ask-")
        self.addCleanup(shutil.rmtree, d, True)
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/r/app/orders.py"}}]}},
            {"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "In orders.py"}}},
            {"type": "stream_event", "parent_tool_use_id": "t", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "subagent"}}},
            {"type": "result", "session_id": "s-1", "total_cost_usd": 0.01, "duration_ms": 900},
        ]
        fake = os.path.join(d, "claude")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$(dirname \"$0\")/args\"\ncat <<'EOF'\n" + "\n".join(json.dumps(e) for e in events) + "\nEOF\n")
        os.chmod(fake, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        got = list(ask.ask("where are orders made?", d, "sys", resume="s-0"))
        self.assertEqual([e["t"] for e in got], ["session", "tool", "text", "done"])
        self.assertEqual(got[1]["d"], "Reading orders.py")
        self.assertEqual(got[2]["d"], "In orders.py")
        self.assertEqual(got[3]["session"], "s-1")
        with open(os.path.join(d, "args")) as fh:
            args = fh.read().split("\n")
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertEqual(args[args.index("--tools") + 1:args.index("--tools") + 4], ["Read", "Grep", "Glob"])
        self.assertEqual(args[args.index("--resume") + 1], "s-0")
        self.assertEqual(args[-2:], ["where are orders made?", ""])


class StyleTest(unittest.TestCase):
    """Stylesheets are reported per rule, like code is per function."""

    def test_rules(self):
        from wiretap.style_extract import extract
        src = "/* top */\n.a { color: red }\n@media (max-width: 600px) {\n  .a { color: blue }\n}\n@keyframes spin { to { rotate: 1turn } }\n"
        names = [(f["name"], f["line"]) for f in extract(src, "s.css")["functions"]]
        self.assertEqual(names, [(".a", 2), ("@media (max-width: 600px) › .a", 4), ("@keyframes spin", 6)])
        self.assertEqual(extract("<p>hi</p>", "index.html")["functions"], [])


class TestHttp(FixtureCase):
    def test_hook_endpoint_and_stream(self):
        state = State(self.config)
        threading.Thread(target=state.hook_worker, daemon=True).start()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{port}"
            g = json.load(urllib.request.urlopen(base + "/api/graph"))
            self.assertGreater(len(g["fns"]), 5)
            self.assertIn(b"Wiretap", urllib.request.urlopen(base + "/").read())
            req = urllib.request.Request(base + "/api/hook", data=json.dumps({
                "session_id": "h1", "hook_event_name": "UserPromptSubmit", "prompt": "hello"}).encode(),
                headers={"Content-Type": "application/json"})
            self.assertEqual(json.load(urllib.request.urlopen(req)), {"ok": True})
            for _ in range(50):
                s = json.load(urllib.request.urlopen(base + "/api/session"))
                if s["session"]:
                    break
                time.sleep(0.05)
            self.assertEqual(s["session"]["tasks"][-1]["prompt"], "hello")
            src = json.load(urllib.request.urlopen(base + "/api/source?file=f:api:app/services/pricing.py&start=4&end=6"))
            self.assertEqual(src["lines"][0], "def compute_total(items, method=None):")
            tr = json.load(urllib.request.urlopen(base + "/api/search?q=cod"))
            self.assertTrue(any(h["id"].endswith("#pay_later_fee") for h in tr["hits"]))
        finally:
            srv.shutdown()

    def test_serves_the_layout_script_and_nothing_outside_web(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(State(self.config)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            r = urllib.request.urlopen(base + "/web/vendor/elk/elk-api.js")
            self.assertIn("javascript", r.headers["Content-Type"])
            for bad in ["/web/../wiretap/server.py", "/web/%2e%2e/wiretap/server.py", "/web/vendor/elk/LICENSE.md"]:
                with self.assertRaises(urllib.error.HTTPError, msg=bad) as e:
                    urllib.request.urlopen(base + bad)
                self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()



class ShellReadTest(unittest.TestCase):
    """Claude often reads code through Bash (grep, sed -n) instead of the Read tool."""

    def test_named_and_hits(self):
        from wiretap.session import shell_files
        named, hits, lines, search, ranges = shell_files("cd /r/app && sed -n 10,40p routers/a.py | head", "/x")
        self.assertEqual(named, ["/r/app/routers/a.py"])
        self.assertEqual((hits, lines, search), ([], {}, None))
        self.assertEqual(ranges, {"/r/app/routers/a.py": [(10, 40)]})
        named, hits, lines, search, _ = shell_files('grep -rnE "cart_items|order_id" --include=*.py app/routers', "/r",
                                                 "./app/b.py:12: x\napp/c.ts\nBinary file d matches")
        self.assertEqual(hits, ["/r/app/b.py", "/r/app/c.ts"])
        self.assertEqual(lines, {"/r/app/b.py": [(12, 12)]})
        self.assertEqual(search, ("cart_items | order_id", "app/routers"))
        _, _, lines, _, _ = shell_files("grep -n driver admin.py | head -5", "/r", "1875:    driver = x\n")
        self.assertEqual(lines, {"/r/admin.py": [(1875, 1875)]})
        _, hits, _, _, _ = shell_files("sed -n 1,5p a.py", "/r", "app/b.py: not a hit\n")
        self.assertEqual(hits, [])
        from wiretap.session import final_cwd
        self.assertEqual(final_cwd("cd /r/app && ls; cd sub", "/x"), "/r/app/sub")
        # redirections are not search locations
        *_, search, _ = shell_files('grep -rl "a|b" . 2>/dev/null', "/r")
        self.assertEqual(search, ("a | b", ""))


if __name__ == "__main__":
    unittest.main()
