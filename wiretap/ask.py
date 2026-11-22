"""Ask: a question about the project, answered by `claude -p` (Sonnet, high effort) as it writes.

Claude runs in the project folder with read-only tools (Read, Grep, Glob), so it can look at the code but never change
it. The answer streams back as small events; a follow-up resumes the same Claude session. Costs tokens only when asked.
"""

import json
import os
import subprocess

TOOLS = ["Read", "Grep", "Glob"]
TIMEOUT = 600  # seconds for one answer

SYSTEM = """You are answering questions inside Wiretap, a live map of the codebase at {root}.
You can read the code with Read, Grep and Glob, but you can't change anything or run commands.
The project is "{name}": {stats}. Its apps: {apps}.
{focus}Answer what was asked, in plain words, short first and detail after. Name files as path:line so they can be found.
When the question is about the code, look before answering; when it isn't, just answer."""


def system_prompt(graph, focus=None):
    st = graph.get("stats", {})
    apps = "; ".join(f"{a['name']} ({a.get('lang', '?')}, in {a.get('root') or '.'})" for a in graph.get("apps", [])) or "none found"
    stats = f"{st.get('files', 0)} files, {st.get('functions', 0)} functions, {st.get('edges', 0)} links"
    focus = f"The user is looking at {focus} on the map.\n" if focus else ""
    return SYSTEM.format(root=graph.get("root", "."), name=graph.get("name", "project"), stats=stats, apps=apps, focus=focus)


def _tool_line(name, inp):
    """A short line for a tool call: Read graph.py, Grep "order_id", Glob **/*.ts."""
    if name == "Read":
        return f"Reading {os.path.basename(inp.get('file_path', ''))}"
    if name == "Grep":
        return f"Searching for “{inp.get('pattern', '')}”"
    if name == "Glob":
        return f"Listing {inp.get('pattern', '')}"
    return name


def ask(question, root, system, resume=None, model="sonnet", effort="high"):
    """Yield events: {"t": "session" | "text" | "tool" | "error", "d"}, {"t": "wait"}, {"t": "done", "session", "cost", "ms"}.

    Closing the generator (the browser stopped listening) kills the claude process.
    """
    args = ["claude", "-p", "--model", model, "--effort", effort, "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--tools", *TOOLS, "--allowedTools", *TOOLS, "--append-system-prompt", system]
    if resume:
        args += ["--resume", resume]
    env = dict(os.environ, WIRETAP_SKIP="1")  # its own reads aren't the user's session
    try:
        proc = subprocess.Popen(args + ["--", question], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, cwd=root, env=env)
    except FileNotFoundError:
        yield {"t": "error", "d": "The claude command was not found on PATH."}
        return
    done = wrote = False
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system":
                # the session id comes first, so a stopped answer can still be followed up; the rest show it's alive,
                # and writing them is how a closed browser is noticed while Claude thinks
                yield {"t": "session", "d": ev["session_id"]} if ev.get("subtype") == "init" else {"t": "wait"}
            elif kind == "stream_event" and not ev.get("parent_tool_use_id"):
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                    yield {"t": "text", "d": e["delta"]["text"]}
                    wrote = True
                elif e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "text" and wrote:
                    yield {"t": "text", "d": "\n\n"}  # text after a tool call starts a new paragraph
            elif kind == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        yield {"t": "tool", "d": _tool_line(b.get("name", ""), b.get("input") or {})}
            elif kind == "result":
                done = True
                if ev.get("is_error"):
                    yield {"t": "error", "d": str(ev.get("result") or "claude -p failed.")[-800:]}
                yield {"t": "done", "session": ev.get("session_id"), "cost": ev.get("total_cost_usd"), "ms": ev.get("duration_ms")}
        proc.wait(timeout=TIMEOUT)
        if not done:
            yield {"t": "error", "d": (proc.stderr.read() or "claude -p stopped without an answer.").strip()[-800:]}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
