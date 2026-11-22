"""Plain-English layer over the parsed map (docs/decisions/0006-simplify-layer.md).

The parser owns the structure; a model only names it. One `claude -p --model haiku` call describes one file and the
functions the parser found in it, and each description is cached against that function's hash, so only changed code
is described again. A flow is cut into steps by layer here; the model only names the steps.
"""

import hashlib
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

FORMAT = 3  # bumped when the prompt changes, so old names are written again
TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_SOURCE = 60000
NAME_MAX, LINE_MAX = 48, 180
# calls in flight at once: answers are mostly the model writing, so more at once is the main speed-up
CHUNK = 20  # functions per call
WORKERS = max(1, int(os.environ.get("WIRETAP_DESCRIBE_WORKERS") or 8))
LAYER_LABEL = {"start": "Where it starts", "screen": "Screens", "ui": "Components", "style": "Styles", "client": "Client logic",
               "route": "API routes", "logic": "Backend logic", "data": "Data", "outside": "Outside"}

FILE_PROMPT = """You are labelling a map of a codebase so that someone new to it can read it. On stdin is one source file, with line numbers.
Reply with JSON only, in exactly this shape:
{{"file": {{"name": "...", "line": "..."}}, "functions": {{"<function name exactly as listed>": {{"name": "...", "line": "..."}}}}}}
- Function name: 2 to 4 plain words starting with a verb ending in s, like "Saves delivery partner" or "Finds who calls it".
- File name: 2 to 4 plain words naming what the file is, like "Order endpoints" or "Checkout screen".
- Say what it gives the user or the rest of the app, not how it works. Avoid vague verbs (handles, manages, processes) and code words.
- line: one sentence under 15 words that adds what the name leaves out.
- Say what the code does today, not what its name promises. A stub, a placeholder or code that only returns a fixed answer says so ("For now only replies OK").
- Never invent examples, values or callers that aren't in the code.
- Don't just restate the code's name: send_email -> "Emails the customer a receipt", not "Sends email".
- Functions in one file should read as a set: no two get the same name, and each says what sets it apart.
- Plain words over jargon: a request the app receives (not endpoint), a check every request passes (not middleware), a saved copy (not cache), sign-in (not auth), runs on a timetable (not cron).
- Describe only the functions listed below, using their names exactly as written.

File: {where}
Functions (name: lines):
{fns}"""

FLOW_PROMPT = """Below are the steps of one flow through an app, in order, from where it starts to where it ends. Each step lists what is in it, with a short description.
Reply with JSON only, in exactly this shape:
{{"title": "...", "steps": ["...", "..."]}}
- title: one sentence under 14 words saying what the flow does, like "Admin changes the delivery partner on an order".
- steps: exactly {n} names, one per step, in the same order. 2 to 5 plain words each, like "Admin picks a partner".

Steps:
{steps}"""


class DescribeError(Exception):
    pass


def claude_haiku(prompt, stdin):
    """The default model call. WIRETAP_SKIP keeps the run off the Live view (ADR 0001)."""
    env = dict(os.environ, WIRETAP_SKIP="1")
    try:
        proc = subprocess.run(["claude", "-p", "--model", "haiku", prompt], input=stdin, capture_output=True, text=True,
                              timeout=180, env=env, cwd=TOOL)
    except FileNotFoundError:
        raise DescribeError("The claude command was not found on PATH.")
    except subprocess.TimeoutExpired:
        raise DescribeError("claude -p took longer than 3 minutes.")
    if proc.returncode != 0:
        raise DescribeError((proc.stderr or proc.stdout).strip()[-400:] or "claude -p failed.")
    return proc.stdout


def parse_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise DescribeError("The model did not answer with JSON.")
    try:
        return json.loads(text[a:b + 1])
    except ValueError:
        raise DescribeError("The model's JSON could not be read.")


def _words(s, n):
    return " ".join(str(s or "").split())[:n]


def _clean(entry):
    if not isinstance(entry, dict):
        return None
    name = _words(entry.get("name"), NAME_MAX)
    return {"n": name, "t": _words(entry.get("line"), LINE_MAX)} if name else None


def file_key(file_rec, fns):
    """Changes whenever a function in the file changes, or the file grows or shrinks."""
    h = hashlib.sha1(f"{file_rec['rel']}|{file_rec.get('lines', 0)}".encode())
    for f in sorted(fns, key=lambda f: f["line"]):
        h.update(f["hash"].encode())
    return h.hexdigest()[:16]


def layer_of(nid, file_role, node_kind):
    """The column a node sits in: where flow steps are cut."""
    if nid.startswith("tg:"):
        return "start"
    if nid.startswith("fn:"):
        return file_role or "logic"
    kind = node_kind or ""
    return {"route": "route", "table": "data", "resource": "data", "ext": "outside"}.get(kind, "logic")


def cut_steps(items):
    """items: [{id, layer, ...}] in flow order -> steps of consecutive items in one layer. A start is always its own step."""
    steps = []
    for it in items:
        if steps and steps[-1]["layer"] == it["layer"] and it["layer"] != "start":
            steps[-1]["items"].append(it)
        else:
            steps.append({"layer": it["layer"], "items": [it]})
    return steps


class Describer:
    """Descriptions for one project, cached in describe.json beside the extraction cache."""

    def __init__(self, cache_dir, run=None, workers=WORKERS):
        self.path = os.path.join(cache_dir, "describe.json")
        self.run = run or claude_haiku
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(workers)
        self.busy = set()
        self.data = {"format": FORMAT, "fns": {}, "files": {}, "flows": {}}
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            if data.get("format") == FORMAT:
                self.data = data
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)

    # ---- what is already known ----
    def fresh(self, graph):
        """Descriptions whose function hash (or file key) still matches the current graph."""
        fns_by_file = _fns_by_file(graph)
        out = {"fns": {}, "files": {}}
        with self.lock:
            for f in graph["fns"]:
                e = self.data["fns"].get(f["id"])
                if e and e["h"] == f["hash"]:
                    out["fns"][f["id"]] = {"n": e["n"], "t": e["t"]}
            for fi in graph["files"]:
                e = self.data["files"].get(fi["id"])
                if e and e["k"] == file_key(fi, fns_by_file.get(fi["id"], [])):
                    out["files"][fi["id"]] = {"n": e["n"], "t": e["t"]}
        return out

    def is_stale(self, file_rec, fns):
        with self.lock:
            e = self.data["files"].get(file_rec["id"])
            if not e or e["k"] != file_key(file_rec, fns):
                return True
            return any((self.data["fns"].get(f["id"]) or {}).get("h") != f["hash"] for f in fns)

    # ---- describing ----
    def describe_file(self, job):
        """job: {file, fns, lines, where}. Describes the file and those of its functions whose code changed. A long answer
        is what takes the time, so many functions are split into calls of CHUNK that run side by side."""
        fi, fns = job["file"], job["fns"]
        with self.lock:
            todo = [f for f in fns if (self.data["fns"].get(f["id"]) or {}).get("h") != f["hash"]]
        src = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(job["lines"] or [], 1))[:MAX_SOURCE]

        def ask(part):
            listing = "\n".join(f"- {f['name']}: {f['line']}-{f['end']}" for f in part) or "(none: describe the file only)"
            return parse_json(self.run(FILE_PROMPT.format(where=job["where"], fns=listing), src))
        chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)] or [[]]
        if len(chunks) == 1:
            answers = [ask(chunks[0])]
        else:
            with ThreadPoolExecutor(min(len(chunks), 4)) as ex:
                answers = list(ex.map(ask, chunks))
        got = {}
        for ans in answers:
            if isinstance(ans.get("functions"), dict):
                got.update(ans["functions"])
        out = {"fns": {}, "files": {}}
        with self.lock:
            for f in todo:  # names the parser didn't find are dropped: the model can't add functions
                e = _clean(got.get(f["name"]))
                if e:
                    self.data["fns"][f["id"]] = {**e, "h": f["hash"]}
            for f in fns:
                e = self.data["fns"].get(f["id"])
                if e and e["h"] == f["hash"]:
                    out["fns"][f["id"]] = {"n": e["n"], "t": e["t"]}
            e = _clean(answers[0].get("file")) or {"n": fi["label"], "t": ""}
            self.data["files"][fi["id"]] = {**e, "k": file_key(fi, fns)}
            out["files"][fi["id"]] = e
        self.save()
        return out

    def describe_async(self, jobs, done):
        """Describe files in the background; done(file_id, result, error) is called once per file."""
        queued = []
        with self.lock:
            jobs = [j for j in jobs if j["file"]["id"] not in self.busy]
            self.busy.update(j["file"]["id"] for j in jobs)
        for job in jobs:
            queued.append(job["file"]["id"])
            self.pool.submit(self._one, job, done)
        return queued

    def _one(self, job, done):
        fid = job["file"]["id"]
        try:
            res, err = self.describe_file(job), None
        except DescribeError as e:
            res, err = None, str(e)
        except Exception as e:  # a bad answer must not kill the worker
            res, err = None, f"Describing failed: {e}"
        finally:
            with self.lock:
                self.busy.discard(fid)
        done(fid, res, err)

    def flow(self, steps):
        """steps: cut_steps() output, items carrying {id, label, line, hash}. One call names the steps."""
        key = hashlib.sha1(json.dumps([[(it["id"], it.get("hash", "")) for it in st["items"]] for st in steps]).encode()).hexdigest()[:16]
        with self.lock:
            hit = self.data["flows"].get(key)
        if hit and len(hit["steps"]) == len(steps):
            return hit
        lines = []
        for i, st in enumerate(steps, 1):
            parts = "; ".join(f"{it['label']}" + (f" ({it['line']})" if it.get("line") else "") for it in st["items"])
            lines.append(f"{i}. [{LAYER_LABEL.get(st['layer'], st['layer'])}] {parts}")
        ans = parse_json(self.run(FLOW_PROMPT.format(n=len(steps), steps="\n".join(lines)), ""))
        names = ans.get("steps")
        if not isinstance(names, list) or len(names) != len(steps):
            raise DescribeError(f"The model named {len(names) if isinstance(names, list) else 0} steps, not {len(steps)}.")
        res = {"title": _words(ans.get("title"), LINE_MAX), "steps": [_words(n, NAME_MAX) or "Step" for n in names]}
        with self.lock:
            self.data["flows"][key] = res
        self.save()
        return res


def _fns_by_file(graph):
    out = {}
    for f in graph["fns"]:
        out.setdefault(f["file"], []).append(f)
    return out


def jobs_for(graph, file_ids, text_of, app_names, describer):
    """Describe jobs for the files that need one."""
    files = {f["id"]: f for f in graph["files"]}
    fns_by_file = _fns_by_file(graph)
    jobs = []
    for fid in file_ids:
        fi = files.get(fid)
        if not fi:
            continue
        fns = fns_by_file.get(fid, [])
        if describer.is_stale(fi, fns):
            jobs.append({"file": fi, "fns": fns, "lines": text_of(fid), "where": f"{app_names.get(fi['app'], fi['app'])} · {fi['rel']}"})
    return jobs
