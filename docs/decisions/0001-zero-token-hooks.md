# 0001 · Watching Claude Code costs no tokens

- **Status:** accepted
- **Date:** 2026-11-16

## Context

The Live view should show what Claude Code reads, plans and changes while it works. Two ways to get that: ask Claude to report (a tool or MCP server it calls, instructions in its prompt), or observe it from outside. Reporting costs tokens on every step, changes how Claude works, and stops the moment Claude forgets. The map also must never slow Claude down or break a session when the map isn't running.

## Decision

Observe through Claude Code hooks. Each hooked event runs one shell command: `curl -s -m 1` posting the hook's JSON to `127.0.0.1`, then `exit 0` ([`wiretap/__main__.py:23-26`](../../wiretap/__main__.py#L23-L26)). The server answers before doing any work ([`wiretap/server.py:356-360`](../../wiretap/server.py#L356-L360)). Claude never reads anything from Wiretap. The only model call is the optional **Summarize** button, which runs `claude -p` with `WIRETAP_SKIP=1` so that run doesn't show up on the map itself ([`wiretap/server.py:228-231`](../../wiretap/server.py#L228-L231)).

## Consequences

- Watching is free and invisible to Claude; a stopped server costs at most the curl timeout per event, and in practice a refused connection returns at once.
- Everything Wiretap knows has to be worked out from tool inputs and outputs. Reads done through the shell (`grep -rn`, `sed -n 10,40p`) needed their own parsing ([`wiretap/session.py`](../../wiretap/session.py) `shell_files`), and "what Claude was thinking" is only available as its plan file.
- The hooks live in the user's global settings, so `install-hooks` backs the file up, marks every entry with `# wiretap`, and `uninstall-hooks` removes exactly those.
