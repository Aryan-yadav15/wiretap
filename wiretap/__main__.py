"""Wiretap command line.

  python3 -m wiretap serve            index the project, watch it, open the map at http://127.0.0.1:7878
  python3 -m wiretap index            index once and print what was found
  python3 -m wiretap describe         write plain-English names for the whole project ahead of time (uses claude -p)
  python3 -m wiretap install-hooks    send Claude Code activity to the map (edits ~/.claude/settings.json)
  python3 -m wiretap uninstall-hooks  remove those hooks again
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(HERE, "wiretap.config.json")
MARK = "# wiretap"
# The terminal button (a Claude Code mod), loaded in every session via CLAUDE_CODE_PLUGIN_DIRS.
MOD_DIR = os.path.join(HERE, "claude-mod", "wiretap-live")


def hook_command(port):
    # curl answers in milliseconds even when the server is down, and never blocks Claude Code.
    return (f'[ -n "$WIRETAP_SKIP" ] || curl -s -m 1 -X POST -H "Content-Type: application/json" '
            f'--data-binary @- http://127.0.0.1:{port}/api/hook >/dev/null 2>&1; exit 0 {MARK}')


def hooks_block(port):
    h = [{"type": "command", "command": hook_command(port), "timeout": 5}]
    return {
        "SessionStart": [{"hooks": h}],
        "UserPromptSubmit": [{"hooks": h}],
        "PreToolUse": [{"matcher": "Bash", "hooks": h}],
        "PostToolUse": [{"matcher": "Read|Edit|MultiEdit|Write|NotebookEdit|Bash|ExitPlanMode|Grep|Glob", "hooks": h}],
        "Stop": [{"hooks": h}],
    }


def _strip(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            cmds = [c for c in group.get("hooks", []) if MARK not in c.get("command", "")]
            if cmds:
                kept.append({**group, "hooks": cmds})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    env = settings.get("env", {})
    dirs = [d for d in env.get("CLAUDE_CODE_PLUGIN_DIRS", "").split(os.pathsep) if d and d != MOD_DIR]
    if dirs:
        env["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(dirs)
    else:
        env.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
    if "env" in settings and not env:
        settings.pop("env")
    return settings


def _short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def install(path, port, dry):
    settings = {}
    if os.path.exists(path):
        with open(path) as fh:
            settings = json.load(fh)
    settings = _strip(settings)
    hooks = settings.setdefault("hooks", {})
    for event, groups in hooks_block(port).items():
        hooks.setdefault(event, []).extend(groups)
    env = settings.setdefault("env", {})
    env["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(
        [d for d in env.get("CLAUDE_CODE_PLUGIN_DIRS", "").split(os.pathsep) if d] + [MOD_DIR])
    text = json.dumps(settings, indent=2) + "\n"
    if dry:
        print(text)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = path + ".before-wiretap"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
            print(f"Backed up {path} -> {backup}")
    with open(path, "w") as fh:
        fh.write(text)
    print(f"Installed Wiretap hooks in {_short(path)}. New Claude Code sessions will report to http://127.0.0.1:{port}")
    print("They also get the \"Wiretap live\" button above the prompt and the /wiretap command.")


def uninstall(path):
    if not os.path.exists(path):
        print("Nothing to remove.")
        return
    with open(path) as fh:
        settings = json.load(fh)
    with open(path, "w") as fh:
        fh.write(json.dumps(_strip(settings), indent=2) + "\n")
    print(f"Removed Wiretap hooks and the terminal button from {_short(path)}")


def _missing_folders(config):
    """Print an error for a project root or app folder that doesn't exist (an unedited example config,
    a typo) instead of serving an empty map. Returns True if any is missing."""
    with open(config) as fh:
        cfg = json.load(fh)
    root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config)), cfg.get("root", ".")))
    missing = [root] if not os.path.isdir(root) else [
        p for p in (os.path.join(root, app["root"]) for app in cfg.get("apps", [])) if not os.path.isdir(p)]
    for p in missing:
        print(f"Folder not found: {p}\nSet \"root\" and each app's \"root\" in {config} (see docs/setup.md).", file=sys.stderr)
    return bool(missing)


def explain(p, g, path):
    """Print what one file gave the map: its functions, each adapter's records, and the routes and links made from them."""
    app, rel = p.locate(os.path.abspath(path))
    if not app or rel not in app.extract:
        print(f"{path} is not in any app of this project (or is not a Python / TypeScript file).", file=sys.stderr)
        return 1
    x, fid = app.extract[rel], f"f:{app.id}:{rel}"
    print(f"{rel}  ({app.name}, {x.get('lang')})" + (f"  parse error: {x['error']}" if x.get("error") else ""))
    fns = x.get("functions", [])
    print(f"  functions ({len(fns)}): " + ", ".join(f"{f['name']} :{f['line']}" for f in fns[:30]) + (" …" if len(fns) > 30 else ""))
    show = {
        "routes": lambda r: f"{r['method']} {r['path'] or '/'} -> {r.get('fn') or r.get('target') or '(no handler)'}",
        "includes": lambda r: f"{r['parent']} mounts {r.get('child') or r.get('spec')} at '{r['prefix']}'",
        "tables": lambda r: f"{r['op']} {r['table']} in {r['fn']}",
        "http": lambda r: f"{r['method']} {''.join(q.get('s', '{}') for q in (r.get('url') or []))} from {r['from']}",
        "http_out": lambda r: f"{r['url']} from {r['fn']}",
        "spawns": lambda r: f"{r['kind']}: {r['target']}",
        "resources": lambda r: f"{r['res']} in {r['fn']}",
    }
    for kind, fmt in show.items():
        for r in x.get(kind, []):
            print(f"  {kind:<9} [{r.get('via', 'core')}] {fmt(r)}  (line {r.get('line', '?')})")
    if x.get("model_tables"):
        print("  models    [sqlalchemy] " + ", ".join(f"{k} = {v}" for k, v in x["model_tables"].items()))
    routes = [n["label"] + (f" -> {n['fn'].split('#')[-1]}" if n.get("fn") else "") for n in g["nodes"] if n.get("kind") == "route" and n.get("file") == fid]
    if routes:
        print("  on the map, routes: " + "; ".join(routes))
    own = {f["id"] for f in g["fns"] if f["file"] == fid}
    kinds = {}
    for e in g["edges"]:
        if e["s"] in own:
            kinds[e["k"]] = kinds.get(e["k"], 0) + 1
    print("  on the map, links out: " + (", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "none"))
    lost = [k for k in g["unresolved"] if k[0].startswith(app.id + ":")]
    if lost:
        print(f"  calls this app could not follow (top): " + ", ".join(f"{k.split(': ', 1)[1]} x{n}" for k, n in lost[:8]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wiretap", description="Visual map of how a codebase connects, live while Claude Code works.")
    ap.add_argument("--config", default=os.environ.get("WIRETAP_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="index, watch and serve the map")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open the browser")
    ix = sub.add_parser("index", help="index once and print a report")
    ix.add_argument("--explain", metavar="FILE", help="show what the parser and each adapter found in one file")
    ix.add_argument("--root", metavar="FOLDER", help="map a folder without a config, the way the server does for Claude's folders")
    d = sub.add_parser("describe", help="write plain-English names for Simple mode ahead of time (claude -p, haiku)")
    d.add_argument("--root", metavar="FOLDER", help="a folder without a config, as the server maps Claude's folders")
    d.add_argument("--yes", action="store_true", help="don't ask before calling the model")
    i = sub.add_parser("install-hooks", help="send Claude Code activity to the map")
    i.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    i.add_argument("--port", type=int, default=7878)
    i.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("uninstall-hooks")
    u.add_argument("--settings", default=os.path.expanduser("~/.claude/settings.json"))
    a = ap.parse_args(argv)

    if a.cmd == "install-hooks":
        return install(a.settings, a.port, a.dry_run)
    if a.cmd == "uninstall-hooks":
        return uninstall(a.settings)
    config = a.config if os.path.exists(a.config) else None
    if a.cmd in ("index", "describe") and a.root:
        config = None
    elif not config and (a.cmd in ("index", "describe") or a.config != DEFAULT_CONFIG):
        print(f"No config at {a.config}.\nCopy wiretap.config.example.json to wiretap.config.json and list your project's apps "
              "(see docs/setup.md#configuration).", file=sys.stderr)
        return 1
    if config and a.cmd in (None, "serve", "index", "describe") and _missing_folders(config):
        return 1
    if a.cmd == "describe":
        return describe(a)
    if a.cmd == "index":
        from .graph import Project
        t0 = time.time()
        if a.root:
            import tempfile
            from .graph import autodetect
            cfg, why = autodetect(os.path.abspath(a.root))
            if why:
                print(why, file=sys.stderr)
                return 1
            p = Project(cfg=cfg, cache_dir=os.path.join(tempfile.gettempdir(), "wiretap-index-cache"))
        else:
            p = Project(a.config)
        p.load_cache()
        p.scan()
        p.save_cache()
        g = p.build()
        st = g["stats"]
        print(f"{g['name']}: {st['files']} files, {st['functions']} functions, {st['edges']} links in {time.time() - t0:.1f}s")
        if a.explain:
            return explain(p, g, a.explain)
        from .coverage import lines
        print("What Wiretap understood:")
        for line in lines(g["coverage"]):
            print(line)
        for u in g["unmatched_http"][:20]:
            print(f"    no route: {u['call']}  ({u['app']} {u['file']}:{u['line']})")
        print(f"  Calls the map could not follow: {st.get('unresolved_calls', 0)}")
        errs = [f for f in g["files"] if f.get("error")]
        for f in errs[:20]:
            print(f"  parse error: {f['app']} {f['rel']}: {f['error']}")
        return
    if a.cmd in (None, "serve"):
        from .server import serve
        if getattr(a, "open", False):
            import threading
            import webbrowser
            port = getattr(a, "port", None) or 7878
            threading.Timer(2.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        # without a config, every project is set up the first time Claude Code works in it
        serve(config, getattr(a, "port", None))
        return
    ap.print_help()


def describe(a):
    """Fill the Simple-mode cache for every file whose functions changed since they were last described."""
    import re
    import threading
    from .graph import Project, autodetect
    from .simplify import Describer, jobs_for
    if a.root:
        root = os.path.abspath(a.root)
        cfg, why = autodetect(root)
        if why:
            print(why, file=sys.stderr)
            return 1
        # the same cache folder the server uses for this folder, so the map picks the names up
        pid = re.sub(r"[^a-z0-9]+", "-", os.path.basename(root).lower()).strip("-") or "project"
        p = Project(cfg=cfg, cache_dir=os.path.join(HERE, ".wiretap-cache", "auto", pid))
    else:
        p = Project(a.config)
    p.load_cache()
    p.scan()
    p.save_cache()
    g = p.build()
    d = Describer(p.cache_dir)

    def text_of(fid):
        _, app_id, rel = fid.split(":", 2)
        try:
            with open(os.path.join(p.app_by_id[app_id].root, rel), encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        except OSError:
            return []
    jobs = jobs_for(g, [f["id"] for f in g["files"]], text_of, {x["id"]: x["name"] for x in g["apps"]}, d)
    print(f"{g['name']}: {len(jobs)} of {len(g['files'])} files need a description (one claude -p --model haiku call each).")
    if not jobs:
        return
    if not a.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        return
    left, failed, lock, finished = [len(jobs)], [], threading.Lock(), threading.Event()

    def done(fid, res, err):
        with lock:
            left[0] -= 1
            if err:
                failed.append((fid, err))
            print(f"  {'!' if err else '✓'} {fid.split(':', 2)[2]}{'  ' + err if err else ''}  ({left[0]} left)")
            if not left[0]:
                finished.set()
    d.describe_async(jobs, done)
    finished.wait()
    print(f"Done: {len(jobs) - len(failed)} described, {len(failed)} failed.")
    return 1 if failed else None


if __name__ == "__main__":
    sys.exit(main())
