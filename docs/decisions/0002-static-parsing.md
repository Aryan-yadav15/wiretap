# 0002 · Read code, don't run it

- **Status:** accepted
- **Date:** 2026-11-16

## Context

The map needs calls between functions, API calls matched to routes, and table reads and writes across several apps (web, mobile, backends). Running the apps with tracing would give exact runtime data, but needs every app's environment, secrets and databases, and only shows the paths that were exercised.

## Decision

Parse source files only. Python goes through the standard library `ast` ([`wiretap/py_extract.py`](../../wiretap/py_extract.py)); TypeScript through the TypeScript compiler API without a type checker ([`wiretap/ts_extract.mjs`](../../wiretap/ts_extract.mjs)), run as a Node child process. Per-file results are cached by modification time and size ([`wiretap/graph.py:148`](../../wiretap/graph.py#L148)), and a linker joins them into one graph.

## Consequences

- Works on any checkout with no setup beyond Python and TypeScript. A cold index of a 638-file, six-app project took 1.7 s; after that only changed files are parsed.
- Covers code that rarely runs (admin tools, error paths) as well as hot paths.
- Anything resolved at runtime is invisible: calls through strings, dependency injection, `dispatch`, websocket managers. These are counted and listed in the System view instead of silently dropped ([`wiretap/graph.py`](../../wiretap/graph.py) `unresolved`).
- Without type checking, some method calls are matched by name; hook-returned functions are marked as guesses.
- Runtime questions ("who held the thread pool during the incident") still need runtime data; the Resources view only narrows down who could be involved.
