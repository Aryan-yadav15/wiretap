# 0003 · Diffs per function, not per line

- **Status:** accepted
- **Date:** 2026-11-16

## Context

When Claude edits a file, "+40 −12 in orders.py" doesn't say what changed or what else might break. To show ripple ("these other files call code that changed"), changes have to be known at the level the graph links: functions.

## Decision

The parsers hash each function's source text ([`wiretap/py_extract.py`](../../wiretap/py_extract.py) `_hash`). On an edit, the file is re-parsed and `fn_diff` compares the hashes before and after: added, changed, removed ([`wiretap/session.py:134`](../../wiretap/session.py#L134)). Each task keeps the hashes from when it first touched a file, so the result is the net change for that request. Callers of changed functions are found by walking the graph backwards up to 3 hops ([`wiretap/server.py:31`](../../wiretap/server.py#L31)).

## Consequences

- Live can say "changed `CreateOrder` and `_fire_instant_dispatch`" and light up the files that call them.
- A change outside any function (module-level constants, imports) doesn't show as a function change; the file still shows as edited.
- Ripple is static: it lists who *calls* the changed code, not who is actually affected at runtime.
