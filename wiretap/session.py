"""Turn Claude Code hook events (and plain file changes) into a session timeline with function-level diffs."""

import os
import re
import time
from collections import defaultdict, deque

from .style_extract import STYLE_EXT

TEST_CMD = re.compile(r"\b(pytest|jest|vitest|npm (run )?test|yarn test|pnpm test|python -m unittest|go test|cargo test)\b")
PYTEST_SUMMARY = re.compile(r"(?:(\d+) failed)?[ ,]*(?:(\d+) passed)?[ ,]*(?:(\d+) error)?", re.I)
FILE_MENTION = re.compile(r"[\w@./-]+\.(?:py|tsx|ts|jsx|js|mjs|css|scss|sass|less|html)\b")
# grep/rg output lines start with "path:line:" or are bare paths (grep -l)
HIT_LINE = re.compile(r"^(?:\./)?([\w@./-]+\.(?:py|tsx|ts|jsx|js|mjs|css|scss|sass|less|html))(?::\d+[:-]|:|$)", re.M)
HIT_AT = re.compile(r"^(?:\./)?([\w@./-]+\.(?:py|tsx|ts|jsx|js|mjs|css|scss|sass|less|html))[:-](\d+)[:-]", re.M)
LINE_ONLY = re.compile(r"^(\d+)[:-]", re.M)          # grep -n on a single file
SED_RANGE = re.compile(r"\bsed\s+-n\s+['\"]?(\d+)(?:,(\d+))?p")
HEAD_N = re.compile(r"\bhead\s+-(?:n\s*)?(\d+)\b")
# a subagent's report coming back: Claude Code delivers it as a prompt, but nobody typed it
HANDBACK = re.compile(r"^\s*(?:Another Claude session sent a message:\s*)?<agent-message\b")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def strip_heredocs(cmd):
    """The command without the text it feeds in (`cat > x.py <<EOF … EOF`), so a file's words aren't read as commands."""
    return HEREDOC.sub("<<…", cmd or "")


def prompt_from_transcript(path):
    """The last prompt the person typed, from a Claude Code transcript (.jsonl). None if unreadable."""
    import json
    last = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    content = (json.loads(line).get("message") or {}).get("content")
                except ValueError:
                    continue
                if isinstance(content, list):
                    if any(c.get("type") == "tool_result" for c in content if isinstance(c, dict)):
                        continue
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<") and not HANDBACK.match(content):
                    last = content.strip()
    except OSError:
        return None
    return last


def split_shell(cmd):
    """Split on && || ; | and newlines, but not inside quotes."""
    parts, cur, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < len(cmd):
                cur.append(c)
                i += 1
                c = cmd[i]
        elif c in "'\"":
            q = c
        elif c in ";|&\n":
            if c == "&" and cmd[i + 1:i + 2] != "&":
                cur.append(c)
                i += 1
                continue
            parts.append("".join(cur))
            cur = []
            if cmd[i + 1:i + 2] == c:
                i += 1
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]
SEARCH_CMD = re.compile(r"^(?:\w+=\S+\s+)*(grep|egrep|rg|ag|ack|git\s+grep)\b")
# options of grep/rg that take a value as the next word
VALUE_OPTS = {"-e", "-f", "-A", "-B", "-C", "-m", "-g", "-t", "-T", "--include", "--exclude", "--exclude-dir", "--glob", "--type", "--max-count"}


def describe_search(part):
    """`grep -rnE "a|b" --include=*.py app/routers` -> ("a | b", "app/routers")."""
    import shlex
    try:
        toks = shlex.split(part)
    except ValueError:
        toks = part.split()
    i = 2 if toks[:2] == ["git", "grep"] else 1
    pattern, where = None, []
    while i < len(toks):
        t = toks[i]
        if t in ("-e", "--regexp") and i + 1 < len(toks):
            pattern = pattern or toks[i + 1]
            i += 2
            continue
        if t in VALUE_OPTS:
            i += 2
            continue
        if t.startswith("-") or re.match(r"^\d*[<>]", t):
            i += 1
            continue
        if pattern is None:
            pattern = t
        else:
            where.append(t)
        i += 1
    if pattern is None:
        return None
    pattern = re.sub(r"\\?\|", " | ", pattern)
    return pattern, " ".join(w.rstrip("/") for w in where if w not in (".", "./"))


def final_cwd(cmd, cwd):
    """The directory a command leaves the shell in (top-level `cd`s only)."""
    for part in split_shell(cmd):
        m = re.match(r"cd\s+(['\"]?)([^'\"]+?)\1$", part.strip())
        if m:
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(m.group(2))))
    return cwd


def shell_files(cmd, cwd, stdout=""):
    """Files a shell command looks at. Returns (named, hits, lines, search):
    named  files named in the command (sed -n 1,40p x.py, cat, head, grep … file),
    hits   files listed in its output (grep -rn / -l, rg, find),
    lines  {file: [(line, line), ...]} grep -n hits,
    search ("pattern", "where") for a grep/rg, else None,
    ranges {file: [(start, end), ...]} line ranges printed (sed -n, head).
    Follows `cd dir &&` so relative paths resolve."""
    named, lines, ranges, search = [], {}, {}, None
    for part in split_shell(cmd):
        part = part.strip()
        m = re.match(r"cd\s+(['\"]?)([^'\"]+)\1\s*$", part)
        if m:
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(m.group(2))))
            continue
        files = [os.path.normpath(os.path.join(cwd, t)) for t in FILE_MENTION.findall(part) if "*" not in t]
        named += files
        if search is None and SEARCH_CMD.match(part):
            search = describe_search(part)
        rng = SED_RANGE.search(part)
        if rng and files:
            a = int(rng.group(1))
            ranges.setdefault(files[-1], []).append((a, int(rng.group(2) or a)))
        rng = HEAD_N.search(part)
        if rng and files and part.lstrip().startswith("head"):
            ranges.setdefault(files[-1], []).append((1, int(rng.group(1))))
    searched = re.search(r"\b(grep|rg|ag|ack|find|fd|ls|git\s+(grep|ls-files|status|diff\s+--name-only))\b", cmd)
    out = stdout or ""
    hits = [os.path.normpath(os.path.join(cwd, m)) for m in HIT_LINE.findall(out)] if searched else []
    if searched:
        for path, ln in HIT_AT.findall(out):
            f = os.path.normpath(os.path.join(cwd, path))
            lines.setdefault(f, []).append((int(ln), int(ln)))
        uniq = list(dict.fromkeys(named))
        if not hits and len(uniq) == 1:
            for ln in LINE_ONLY.findall(out):
                lines.setdefault(uniq[0], []).append((int(ln), int(ln)))
    return list(dict.fromkeys(named)), list(dict.fromkeys(hits)), lines, search, ranges


def fn_diff(old, new):
    """Compare two extractions of the same file -> {qual: 'added'|'changed'|'removed'}"""
    a = {f["name"]: f["hash"] for f in (old or {}).get("functions", [])}
    b = {f["name"]: f["hash"] for f in (new or {}).get("functions", [])}
    out = {}
    for k in b:
        if k not in a:
            out[k] = "added"
        elif a[k] != b[k]:
            out[k] = "changed"
    for k in a:
        if k not in b:
            out[k] = "removed"
    return out


def parse_tests(text):
    text = text or ""
    # pytest: "==== 3 passed in 0.4s ====", or with -q just "94 passed, 2 deselected in 0.99s"
    m = re.search(r"=+ (.*?) in [\d.]+s", text) or re.search(r"^((?:\d+ \w+(?:, )?)*\d+ (?:passed|failed|errors?)\b.*?) in [\d.]+s", text, re.M)
    if m:
        s = m.group(1)
        f = re.search(r"(\d+) failed", s)
        p = re.search(r"(\d+) passed", s)
        e = re.search(r"(\d+) error", s)
        return {"passed": int(p.group(1)) if p else 0, "failed": (int(f.group(1)) if f else 0) + (int(e.group(1)) if e else 0)}
    m = re.search(r"Tests:\s+(?:(\d+) failed, )?(?:\d+ skipped, )?(\d+) passed", text)  # jest / vitest
    if m:
        return {"passed": int(m.group(2)), "failed": int(m.group(1) or 0)}
    m = re.search(r"^[ℹ#] fail (\d+)\s*$", text, re.M)  # node --test (the pass line may be cut off by `tail`)
    if m:
        p = re.search(r"^[ℹ#] pass (\d+)\s*$", text, re.M)
        return {"passed": int(p.group(1)) if p else None, "failed": int(m.group(1))}
    m = re.search(r"Ran (\d+) tests?.*?\n\s*\n?(OK|FAILED \((?:failures|errors)=(\d+))", text, re.S)  # unittest
    if m:
        failed = int(m.group(3) or 0)
        return {"passed": int(m.group(1)) - failed, "failed": failed}
    return None


class Task:
    """One user prompt and everything Claude did for it."""

    def __init__(self, prompt, t):
        self.prompt = prompt
        self.started = t
        self.ended = None
        self.events = []
        self.planned = set()        # file ids
        self.planned_new = []       # paths mentioned in the plan that don't exist yet
        self.touched = {}           # file id -> {"status": edited|created|deleted, "fns": {qual: status}, "add": n, "del": n}
        self.reads = set()
        self.looked = {}            # file id -> {function: "read" | "found"}, newest last
        self.tests = None
        self.plan_text = None
        self.running = None         # {"text", "t"}: a shell command Claude started that hasn't returned yet
        self.prompt_tried = False   # looked for a missing prompt in the transcript already


class Session:
    def __init__(self, sid, source):
        self.id = sid
        self.source = source        # "claude" | "editor"
        self.started = time.time()
        self.last = self.started
        self.cwd = None
        self.shell_cwd = None       # where Claude's shell is after its last `cd`
        self.tasks = []
        self.last_shell = 0

    def task(self, t):
        if not self.tasks:
            self.tasks.append(Task(None, t))
        return self.tasks[-1]


class Sessions:
    def __init__(self, project):
        self.p = project
        self.by_id = {}
        self.order = deque(maxlen=30)
        self.recent_edits = {}      # file id -> time, so the watcher doesn't double count hook edits

    def get(self, sid, source="claude"):
        s = self.by_id.get(sid)
        if not s:
            s = self.by_id[sid] = Session(sid, source)
            self.order.append(sid)
        return s

    def latest(self):
        live = [self.by_id[i] for i in self.order if i in self.by_id]
        return max(live, key=lambda s: s.last) if live else None

    # ---- files ----
    def file_id(self, path):
        app, rel = self.p.locate(path)
        if app and rel not in app.extract and not os.path.exists(path):
            # a path resolved against the wrong directory: use the one indexed file with that name, if unique
            name = "/" + os.path.basename(path)
            hits = [(a, r) for a in self.p.apps for r in a.extract if ("/" + r).endswith(name)]
            app, rel = hits[0] if len(hits) == 1 else (None, None)
        return (f"f:{app.id}:{rel.replace(os.sep, '/')}", app, rel) if app else (None, None, None)

    def fns_at(self, app, rel, ranges):
        """Functions covering the given (start, end) line ranges: the enclosing function
        (an endpoint, not its nested helpers), unless that one is huge (a whole screen
        component), then the inner one. A range that spans many functions names none."""
        fns = [f for f in ((app.extract.get(rel) or {}).get("functions", []) if app else []) if f.get("line") and f.get("end")]
        out = []
        for a, b in ranges:
            over = [f for f in fns if f["line"] <= b and f["end"] >= a]
            outer = [f for f in over if not any(g is not f and g["line"] <= f["line"] and g["end"] >= f["end"] for g in over)]
            picked = []
            for f in outer:
                if f["end"] - f["line"] > 250:
                    inside = [g for g in over if g is not f and f["line"] <= g["line"] and g["end"] <= f["end"]]
                    picked += [g for g in inside if not any(h is not g and h in inside and h["line"] <= g["line"] and h["end"] >= g["end"] for h in inside)] or [f]
                else:
                    picked.append(f)
            # CSS rules are a line or two each, so a short read of a stylesheet still names them
            if 0 < len(picked) <= (12 if rel.endswith(STYLE_EXT) else 6):
                out += [f["name"] for f in picked]
        return list(dict.fromkeys(out))

    def look(self, task, fid, names, how):
        """Credit functions Claude looked at. A narrow look (one function) counts more
        than a broad one, so the function the answer lives in rises to the top."""
        rec = task.looked.pop(fid, {})       # re-insert: newest last
        for n in names:
            r = rec.setdefault(n, {"how": how, "score": 0.0})
            r["score"] += 1.0 / len(names)
            if how == "read":
                r["how"] = "read"
        if rec:
            task.looked[fid] = rec

    def _event(self, sess, task, kind, **kw):
        ev = {"i": len(task.events), "t": time.time(), "k": kind}
        ev.update({k: v for k, v in kw.items() if v is not None})
        task.events.append(ev)
        sess.last = ev["t"]
        return ev

    def apply_change(self, sess, task, fid, old, new, add=None, dele=None, via=None):
        """Record a file change in a task. Returns the event."""
        status = "created" if old is None else ("deleted" if new is None else "edited")
        diff = fn_diff(old, new)
        rec = task.touched.get(fid)
        if rec is None:
            rec = task.touched[fid] = {"status": status, "fns": {}, "add": 0, "del": 0, "baseline": {f["name"]: f["hash"] for f in (old or {}).get("functions", [])}}
        elif status == "deleted":
            rec["status"] = "deleted"
        # net status per function, relative to the file as it was when the task first touched it
        cur = {f["name"]: f["hash"] for f in (new or {}).get("functions", [])}
        base = rec["baseline"]
        net = {}
        for q, h in cur.items():
            if q not in base:
                net[q] = "added"
            elif base[q] != h:
                net[q] = "changed"
        for q in base:
            if q not in cur:
                net[q] = "removed"
        rec["fns"] = net
        rec["add"] += add or 0
        rec["del"] += dele or 0
        off = bool(task.planned) and fid not in task.planned
        return self._event(sess, task, "create" if status == "created" else "delete" if status == "deleted" else "edit",
                           file=fid, fns=[{"name": q, "status": s} for q, s in sorted(diff.items())],
                           add=add, dele=dele, off=off or None, via=via)

    # ---- hook entry point ----
    def on_hook(self, h, rescan):
        """h: Claude Code hook payload. rescan(app, rel) -> (old, new) extraction. Returns True if anything changed."""
        sid = h.get("session_id") or "unknown"
        sess = self.get(sid)
        sess.cwd = h.get("cwd") or sess.cwd
        name = h.get("hook_event_name")
        now = time.time()
        if name == "UserPromptSubmit":
            prompt = re.sub(r"^(?:\[Image[^\]]*\]\s*)+", "", (h.get("prompt") or "").strip())  # pasted-image notes
            if HANDBACK.match(prompt):
                # part of the request Claude is already working on, not a new one
                task = sess.task(now)
                task.ended = None
                first = re.sub(r"<[^>]+>", " ", prompt.split("[Subagent hand-back]")[-1]).strip().splitlines()
                self._event(sess, task, "handback", text=(first[0] if first else "")[:200] or None)
                return True
            t = Task(prompt[:500], now)
            if sess.tasks and not sess.tasks[-1].prompt and not sess.tasks[-1].events:
                sess.tasks[-1] = t  # replace the empty task opened by SessionStart
            else:
                sess.tasks.append(t)
            self._event(sess, t, "prompt", text=prompt[:500])
            return True
        if name == "SessionStart":
            sess.task(now)
            return True
        task = sess.task(now)
        if not task.prompt and not task.prompt_tried and h.get("transcript_path"):
            # the prompt came before Wiretap was listening (or before a restart): read it from the transcript
            task.prompt_tried = True
            task.prompt = (prompt_from_transcript(h["transcript_path"]) or "")[:500] or None
        if name in ("Stop", "SubagentStop"):
            if name == "Stop":
                task.ended = now
                task.running = None
                self._event(sess, task, "stop")
            return True
        if name not in ("PostToolUse", "PreToolUse"):
            return False
        tool = h.get("tool_name") or ""
        ti = h.get("tool_input") or {}
        tr = h.get("tool_response")
        if name == "PreToolUse":
            if tool == "Bash":
                sess.last_shell = now
                # only PostToolUse records the step, so show a slow command (a DB query, a build) while it runs
                task.running = {"text": re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", ti.get("command") or "")[:300], "t": now}
                return True
            return False
        task.running = None

        path = ti.get("file_path") or ti.get("notebook_path")
        if tool == "ExitPlanMode" or (tool in ("Write", "Edit") and path and "/.claude/plans/" in path):
            text = ti.get("plan")
            if not text and path:
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    text = ti.get("content")
            self.set_plan(sess, task, text or "")
            return True
        if tool == "Read" and path:
            fid, app, rel = self.file_id(path)
            seen = []
            if fid:
                task.reads.add(fid)
                if ti.get("offset") or ti.get("limit"):
                    start = int(ti.get("offset") or 1)
                    seen = self.fns_at(app, rel, [(start, start + int(ti.get("limit") or 2000) - 1)])
                    self.look(task, fid, seen, "read")
            self._event(sess, task, "read", file=fid, path=None if fid else path, seen=seen or None)
            return True
        if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit") and path:
            fid, app, rel = self.file_id(path)
            add = dele = 0
            if tool == "Edit":
                add, dele = _lines(ti.get("new_string")), _lines(ti.get("old_string"))
            elif tool == "MultiEdit":
                for e in ti.get("edits", []):
                    add += _lines(e.get("new_string"))
                    dele += _lines(e.get("old_string"))
            elif tool == "Write":
                add = _lines(ti.get("content"))
            if not fid:
                self._event(sess, task, "edit", path=path, add=add, dele=dele, outside=True)
                return True
            old, new = rescan(app, rel)
            self.recent_edits[fid] = now
            self.apply_change(sess, task, fid, old, new, add, dele)
            return True
        if tool == "Bash":
            cmd = ti.get("command") or ""
            sess.last_shell = now
            if TEST_CMD.search(strip_heredocs(cmd)):
                out = ""
                if isinstance(tr, dict):
                    out = (tr.get("stdout") or "") + "\n" + (tr.get("stderr") or "")
                elif isinstance(tr, str):
                    out = tr
                res = parse_tests(out)
                if res or not (task.tests and task.tests.get("failed") is not None):
                    # a later run that prints no summary (`pytest | grep warning`) keeps the last real result
                    task.tests = {"cmd": cmd[:200], **(res or {"passed": None, "failed": None})}
                self._event(sess, task, "test", text=cmd[:200], **(res or {}))
                return True
            out = (tr.get("stdout") or "") if isinstance(tr, dict) else (tr if isinstance(tr, str) else "")
            if re.search(r"\bgit\s+commit\b", strip_heredocs(cmd)):
                # a commit lists files in its output, which would otherwise read as search hits
                sess.shell_cwd = final_cwd(cmd, sess.shell_cwd or sess.cwd or "/")
                self._event(sess, task, "run", text=re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)[:2000])
                return True
            # Claude Code's shell keeps its directory between commands, so follow `cd` across calls
            base = sess.shell_cwd or sess.cwd or "/"
            named, hits, lines, search, ranges = shell_files(cmd, base, out)
            sess.shell_cwd = final_cwd(cmd, base)
            ids = {p: self.file_id(p) for p in set(named) | set(hits) | set(lines) | set(ranges)}

            def fns_by_file(by_path):
                acc = {}
                for p, rs in by_path.items():
                    fid, app, rel = ids[p]
                    if fid:
                        acc.setdefault(fid, []).extend(self.fns_at(app, rel, rs))
                return acc
            seen_in, read_in = fns_by_file(lines), fns_by_file(ranges)
            named = list(dict.fromkeys(f for f in (ids[p][0] for p in named) if f))
            hits = [f for f in dict.fromkeys(ids[p][0] for p in hits) if f]
            # kept long: the page reads table names out of DB queries, which come after the connection setup
            text = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)[:2000]
            if search:
                # sed/head reads sharing the command with a grep get their own read events
                for fid, names in read_in.items():
                    names = list(dict.fromkeys(names))
                    task.reads.add(fid)
                    self.look(task, fid, names, "read")
                    self._event(sess, task, "read", file=fid, via="shell", seen=names or None)
                # a search: grep -n hits name the functions the words were found in
                found = list(dict.fromkeys(hits + named))
                for fid in found:
                    task.reads.add(fid)
                for fid, names in seen_in.items():
                    names = list(dict.fromkeys(names))
                    if len(found) <= 6 and len(names) <= 5:   # a search matching everywhere says nothing
                        self.look(task, fid, names, "found")
                self._event(sess, task, "search", text=text, pattern=search[0][:120], where=search[1][:80] or None,
                            files=found[:20], n=len(found),
                            seen=[{"file": f, "names": n[:4]} for f, n in seen_in.items() if n][:6] or None)
                return True
            for fid in named:
                task.reads.add(fid)
                names = list(dict.fromkeys(read_in.get(fid, []) + seen_in.get(fid, [])))
                self.look(task, fid, names, "read")
                self._event(sess, task, "read", file=fid, via="shell", seen=names or None)
            if hits:
                task.reads.update(hits[:8])
                self._event(sess, task, "search", text=text, files=hits[:20], n=len(hits))
            elif not named:
                self._event(sess, task, "run", text=text)
            return True
        if tool in ("Grep", "Glob"):
            names = tr.get("filenames") if isinstance(tr, dict) else None
            found = [f for f in dict.fromkeys(self.file_id(p)[0] for p in (names or [])) if f]
            task.reads.update(found[:8])
            where = ti.get("path") or ti.get("glob") or ""
            if where.startswith(sess.cwd or "\0"):
                where = os.path.relpath(where, sess.cwd)
            self._event(sess, task, "search", text=(ti.get("pattern") or "")[:120], pattern=(ti.get("pattern") or "")[:120],
                        where=where[:80] or None, files=found[:20] or None, n=len(found) or None)
            return True
        return False

    def set_plan(self, sess, task, text):
        task.plan_text = text[:4000]
        mentioned = set(FILE_MENTION.findall(text))
        all_files = [(f"f:{a.id}:{rel.replace(os.sep, '/')}", rel.replace(os.sep, "/")) for a in self.p.apps for rel in a.extract]
        by_base = defaultdict(list)
        for fid, rel in all_files:
            by_base[os.path.basename(rel)].append(fid)
        planned, new = set(), []
        for m in mentioned:
            m2 = m.lstrip("./")
            hits = [fid for fid, rel in all_files if rel.endswith(m2) and (rel == m2 or rel.endswith("/" + m2))]
            if not hits and "/" not in m2:
                hits = by_base.get(m2, [])
                if len(hits) > 3:
                    hits = []
            if hits:
                planned.update(hits)
            else:
                new.append(m2)
        task.planned = planned
        task.planned_new = sorted(set(new))[:30]
        self._event(sess, task, "plan", text=text[:600], files=sorted(planned), new=task.planned_new)

    # ---- plain file changes seen by the watcher ----
    def on_file_change(self, app, rel, old, new):
        fid = f"f:{app.id}:{rel.replace(os.sep, '/')}"
        now = time.time()
        if now - self.recent_edits.get(fid, 0) < 5:
            return False
        # A shell command run by Claude in the last few seconds probably made this change
        latest = self.latest()
        if latest and latest.source == "claude" and now - latest.last_shell < 15:
            self.apply_change(latest, latest.task(now), fid, old, new, via="shell")
            return True
        sess = self.get("editor", source="editor")
        task = sess.task(now)
        if not task.prompt:
            task.prompt = "Changes made outside Claude"
        self.apply_change(sess, task, fid, old, new, via="editor")
        return True

    # ---- views ----
    def snapshot(self, sess, graph_index):
        return {
            "id": sess.id, "source": sess.source, "started": sess.started, "last": sess.last, "cwd": sess.cwd,
            "tasks": [self.task_view(t, graph_index) for t in sess.tasks],
        }

    def task_view(self, t, gi):
        changed_fns = []
        for fid, rec in t.touched.items():
            for q, s in rec["fns"].items():
                if s != "removed":
                    changed_fns.append(fid.replace("f:", "fn:", 1) + "#" + q)
        ripple_fns, ripple_files = gi.upstream(changed_fns, depth=3, exclude_files=set(t.touched)) if gi else (set(), set())
        off = sorted(f for f in t.touched if t.planned and f not in t.planned)
        # with no plan to compare against, flag changed files nothing else in the change connects to
        stray = sorted(gi.stray([f for f, r in t.touched.items() if r["status"] != "deleted"]) - set(off)) if gi and not t.planned else []
        fn_counts = defaultdict(int)
        for rec in t.touched.values():
            for s in rec["fns"].values():
                fn_counts[s] += 1
        return {
            "prompt": t.prompt, "started": t.started, "ended": t.ended,
            "events": t.events, "planned": sorted(t.planned), "planned_new": t.planned_new, "plan": t.plan_text,
            "touched": {f: {k: v for k, v in r.items() if k != "baseline"} for f, r in t.touched.items()},
            "reads": sorted(t.reads), "tests": t.tests, "running": t.running,
            "looked": [{"file": f, "fns": [{"name": q, "how": r["how"], "score": round(r["score"], 2)} for q, r in rec.items()]} for f, rec in t.looked.items()],
            "ripple_files": sorted(ripple_files), "ripple_fns": sorted(ripple_fns)[:400],
            "summary": {
                "edited": sum(1 for r in t.touched.values() if r["status"] == "edited"),
                "created": sum(1 for r in t.touched.values() if r["status"] == "created"),
                "deleted": sum(1 for r in t.touched.values() if r["status"] == "deleted"),
                "fns": dict(fn_counts), "ripple_files": len(ripple_files), "ripple_fns": len(ripple_fns),
                "off_plan": off, "stray": stray, "reads": len(t.reads), "tests": t.tests,
                "lines_added": sum(r["add"] for r in t.touched.values()), "lines_removed": sum(r["del"] for r in t.touched.values()),
            },
        }


def _lines(s):
    return (s.count("\n") + 1) if s else 0
