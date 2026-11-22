"""What Wiretap understood in a project, and what it didn't: per app, the routes, tables, HTTP calls and jobs each
adapter found and how many calls it could follow; for the project, code in languages it doesn't parse and
libraries it doesn't read. Shown by `index`, at server start and in the System view, so a gap is never silent."""

import json
import os
import re
from collections import Counter, defaultdict

from .adapters import NOT_READ

OTHER_LANGS = {
    ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".rs": "Rust",
    ".swift": "Swift", ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir", ".dart": "Dart", ".vue": "Vue",
    ".svelte": "Svelte", ".cpp": "C++", ".c": "C",
}
# record list in an extraction -> what it is, for the report
KINDS = {"routes": "routes", "tables": "tables", "http": "http", "http_out": "http", "spawns": "jobs"}
SKIP = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build", "android", "ios", "vendor", "target", "Pods"}


def _found(app):
    found = defaultdict(Counter)
    for x in app.extract.values():
        for key, kind in KINDS.items():
            for rec in x.get(key, []):
                found[kind][rec.get("via", "core")] += 1
        if x.get("model_tables"):
            found["tables"]["sqlalchemy"] += len(x["model_tables"])
    return {k: dict(v) for k, v in found.items()}


def _deps(app):
    """Dependency names an app declares (package.json, requirements*.txt, pyproject.toml, Pipfile)."""
    names = set()
    try:
        with open(os.path.join(app.root, "package.json")) as fh:
            pkg = json.load(fh)
        for k in ("dependencies", "devDependencies", "peerDependencies"):
            names |= set(pkg.get(k) or {})
    except (OSError, ValueError):
        pass
    for f in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile"):
        try:
            with open(os.path.join(app.root, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read().lower()
        except OSError:
            continue
        names |= set(re.findall(r"^\s*[\"']?([a-z0-9][a-z0-9._-]*)", text, re.M))
    return names


def _other_code(root, limit=20000):
    counts, seen = Counter(), 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP and not d.startswith(".")]
        for f in fns:
            seen += 1
            ext = os.path.splitext(f)[1].lower()
            if ext in OTHER_LANGS:
                counts[ext] += 1
        if seen > limit:
            break
    return counts


def report(project, linker):
    apps = []
    for app in project.apps:
        files = [f for f in linker.files if f["app"] == app.id]
        c = linker.per_app[app.id]
        http_calls = sum(len(x.get("http", [])) for x in app.extract.values())
        apps.append({
            "id": app.id, "name": app.name, "lang": app.lang, "files": len(files),
            "functions": sum(len(x.get("functions", [])) for x in app.extract.values()),
            "errors": sum(1 for f in files if f.get("error")),
            "found": _found(app),
            "http": {"calls": http_calls, "matched": c["http_matched"], "unmatched": c["http_unmatched"], "unknown_url": c["http_unknown_url"]},
            "calls_not_followed": c["unresolved_calls"],
            "guessed_prefix": linker.guessed_prefix.get(app.id),
        })
    other = Counter()
    if not hasattr(project, "other_code"):
        project.other_code = _other_code(project.root)  # once: the map is rebuilt on every save
    for ext, n in project.other_code.items():
        other[OTHER_LANGS[ext]] += n
    not_read = []
    for app in project.apps:
        deps = _deps(app)
        for dep, what in {**NOT_READ["ts"], **NOT_READ["py"]}.items():
            if dep in deps:
                not_read.append({"app": app.id, "dep": dep, "what": what})
    return {"apps": apps, "not_parsed": [{"lang": k, "files": n} for k, n in other.most_common()], "not_read": not_read}


def _n(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def lines(cov):
    """The report as plain text lines, for the terminal."""
    out = []
    for a in cov["apps"]:
        found = a["found"]
        parts = []
        for kind in ("routes", "tables", "http", "jobs"):
            if found.get(kind):
                parts.append(f"{sum(found[kind].values())} {kind if kind != 'http' else 'HTTP calls'} ({', '.join(f'{k} {n}' for k, n in sorted(found[kind].items()))})")
        out.append(f"  {a['name']}: {_n(a['files'], 'file')}, {_n(a['functions'], 'function')}" + (f"; {'; '.join(parts)}" if parts else "; no routes, tables or HTTP calls found"))
        h = a["http"]
        if h["calls"]:
            out.append(f"    HTTP calls matched to a route: {h['matched']} of {h['calls']}" + (f" ({h['unknown_url']} with a URL built at runtime)" if h["unknown_url"] else ""))
        if a.get("guessed_prefix"):
            out.append(f"    Its calls leave out {a['guessed_prefix']} (likely in the client's base URL), so Wiretap adds it. Set \"pathPrefix\" in a config to change that.")
        if a["errors"]:
            out.append(f"    {_n(a['errors'], 'file')} could not be parsed")
    for o in cov["not_parsed"]:
        out.append(f"  Not parsed: {_n(o['files'], o['lang'] + ' file')} (Wiretap reads Python and TypeScript/JavaScript)")
    for n in cov["not_read"]:
        out.append(f"  Not read yet: {n['what']} ({n['dep']} in {n['app']}). See docs/adapters.md to add it.")
    return out
