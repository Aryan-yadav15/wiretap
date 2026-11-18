"""Stylesheets and HTML: each CSS rule counts as a "function" (named by its selector), so reads and edits of a
stylesheet are reported per rule, the same way code is reported per function. HTML files are listed with no rules."""

import bisect
import hashlib

STYLE_EXT = (".css", ".scss", ".sass", ".less")
MARKUP_EXT = (".html",)
GROUPS = ("@media", "@supports", "@layer", "@container", "@document")


def extract(src, rel):
    out = {"lang": "style", "rel": rel, "functions": [], "lines": src.count("\n") + 1,
           # the TypeScript linker reads these keys from every file in the app
           "imports": [], "reexports": [], "exports": {}, "defaultExport": None, "calls": [], "http": [],
           "renders": [], "triggers": [], "consts": {}, "destructured": []}
    if not rel.endswith(STYLE_EXT):
        return out
    starts = [0] + [i + 1 for i, c in enumerate(src) if c == "\n"]
    line_at = lambda i: bisect.bisect_right(starts, i)
    stack, seen = [], {}
    pre_start, i, n = 0, 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if not src[pre_start:i].strip():
                pre_start = n if j < 0 else j + 2  # a comment before a selector is not part of its name
            i = n if j < 0 else j + 2
            continue
        if src.startswith("//", i) and not rel.endswith(".css") and (i == 0 or src[i - 1] != ":"):
            j = src.find("\n", i)
            if not src[pre_start:i].strip():
                pre_start = n if j < 0 else j
            i = n if j < 0 else j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if c == "{":
            raw = src[pre_start:i]
            pre = " ".join(raw.split())
            lead = len(raw) - len(raw.lstrip())
            stack.append({"pre": pre, "line": line_at(pre_start + lead), "body": i + 1})
            pre_start = i + 1
        elif c == "}" and stack:
            b = stack.pop()
            pre_start = i + 1
            parents = [s["pre"] for s in stack]
            in_keyframes = any(p.startswith(("@keyframes", "@-webkit-keyframes")) for p in parents)
            if b["pre"] and not b["pre"].startswith(GROUPS) and not in_keyframes:
                name = " › ".join([p for p in parents if p.startswith(GROUPS)] + [b["pre"]])[:120]
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name} ({seen[name]})"
                out["functions"].append({"name": name, "line": b["line"], "end": line_at(i),
                                         "hash": hashlib.md5(src[b["body"]:i].encode()).hexdigest()[:12]})
        elif c == ";":
            pre_start = i + 1
        i += 1
    return out
