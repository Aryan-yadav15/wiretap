"""Replay a Claude Code transcript into a Wiretap server as hook events, at the session's real pace.

  python3 docs/demo/replay.py ~/.claude/projects/<project>/<session>.jsonl 7899 first

Used for docs/demo.gif: start `python3 -m wiretap serve --port 7899`, replay a session, then record
docs/demo/steps.json with repo-polish (W=1440 H=900 GW=960 FPS=8 COLORS=64 node tools/gif/record.mjs ...).
"""
import json, sys, time, urllib.request
from datetime import datetime
path, port = sys.argv[1], sys.argv[2]
only_first = len(sys.argv) > 3
sid = "demo-replay"
def post(h):
    h.setdefault("session_id", sid)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/hook", json.dumps(h).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
pending, prompts = {}, 0
last = None
cwd = None
for line in open(path):
    d = json.loads(line); m = d.get("message") or {}; cwd = d.get("cwd") or cwd
    if only_first and prompts and d.get("type") == "user" and isinstance(m.get("content"), str) and not m["content"].startswith("<"):
        break
    # keep the session's real pace, so its duration on the page is true
    if d.get("timestamp"):
        t = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
        if last is not None and t > last: time.sleep(t - last)
        last = t
    if d.get("type") == "user" and isinstance(m.get("content"), str) and not m["content"].startswith("<"):
        prompts += 1
        if only_first and prompts > 1: break
        if prompts == 1: post({"hook_event_name": "SessionStart", "cwd": cwd})
        else: post({"hook_event_name": "Stop", "cwd": cwd})
        post({"hook_event_name": "UserPromptSubmit", "cwd": cwd, "prompt": m["content"].replace("chenge", "change")})
    elif d.get("type") == "assistant" and isinstance(m.get("content"), list):
        for c in m["content"]:
            if c.get("type") == "tool_use":
                pending[c["id"]] = c
                if c["name"] == "Bash": post({"hook_event_name": "PreToolUse", "cwd": cwd, "tool_name": "Bash", "tool_input": c["input"]})
    elif d.get("type") == "user" and isinstance(m.get("content"), list):
        for c in m["content"]:
            u = pending.pop(c.get("tool_use_id"), None) if c.get("type") == "tool_result" else None
            if u:
                post({"hook_event_name": "PostToolUse", "cwd": cwd, "tool_name": u["name"], "tool_input": u["input"], "tool_response": d.get("toolUseResult")})
post({"hook_event_name": "Stop", "cwd": cwd})
print("replayed", prompts, "prompt(s)")
