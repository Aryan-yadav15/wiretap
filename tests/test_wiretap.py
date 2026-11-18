"""End-to-end tests on a small fixture project: indexing, linking, and a simulated Claude Code session.

Run:  python3 -m unittest discover -s tests -v
TypeScript is found in a node_modules folder; set WIRETAP_TYPESCRIPT=/path/to/node_modules/typescript if needed.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from wiretap.graph import Project  # noqa: E402
from wiretap.server import State, make_handler, term_regex  # noqa: E402
from wiretap.session import parse_tests  # noqa: E402
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


class TestHelpers(unittest.TestCase):
    def test_term_regex(self):
        rx = term_regex(["cod", "pay_later"])
        for s in ["cod", "COD", "isCod", "cod_fee", "pay_later_fee"]:
            self.assertTrue(rx.search(s), s)
        for s in ["code", "decode", "promo_code", "PROMO_CODE", "Codec"]:
            self.assertFalse(rx.search(s), s)

    def test_parse_tests(self):
        self.assertEqual(parse_tests("==== 1 failed, 212 passed in 3.2s ===="), {"passed": 212, "failed": 1})
        self.assertEqual(parse_tests("Tests:       2 failed, 40 passed, 42 total"), {"passed": 40, "failed": 2})
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
