"""Wiretap command line.

  python3 -m wiretap serve            index the project, watch it, open the map at http://127.0.0.1:7878
  python3 -m wiretap index            index once and print what was found
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wiretap", description="Visual map of how a codebase connects, live while Claude Code works.")
    ap.add_argument("--config", default=os.environ.get("WIRETAP_CONFIG", DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="index, watch and serve the map")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="open the browser")
    sub.add_parser("index", help="index once and print a report")
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
    if a.cmd in (None, "serve", "index") and not os.path.exists(a.config):
        print(f"No config at {a.config}.\nCopy wiretap.config.example.json to wiretap.config.json and list your project's apps "
              "(see \"Configure your project\" in the README).", file=sys.stderr)
        return 1
    if a.cmd == "index":
        from .graph import Project
        t0 = time.time()
        p = Project(a.config)
        p.load_cache()
        p.scan()
        p.save_cache()
        g = p.build()
        st = g["stats"]
        print(f"{g['name']}: {st['files']} files, {st['functions']} functions, {st['edges']} links in {time.time() - t0:.1f}s")
        for app in g["apps"]:
            n = sum(1 for f in g["files"] if f["app"] == app["id"])
            print(f"  {app['name']:<24} {n:>4} files")
        print(f"  API calls matched to a route: {st.get('http_matched', 0)}, unmatched: {st.get('http_unmatched', 0)}")
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
        serve(a.config, getattr(a, "port", None))
        return
    ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
