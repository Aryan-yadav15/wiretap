# 0005 · One adapter per framework, and say what wasn't read

- **Status:** accepted
- **Date:** 2026-11-21

## Context

Wiretap started on one project: FastAPI backends with Supabase, and React / React Native clients. The rules for those stacks lived inside the parsers and the linker. To be useful on other people's projects it needs Express, Next.js, Flask, Prisma and more, added by people who don't know the linker. And on a project it reads only partly, a missing route looks the same as a route that isn't there.

## Decision

- The core parsers ([`py_extract.py`](../../wiretap/py_extract.py), [`ts_extract.mjs`](../../wiretap/ts_extract.mjs)) keep what every file has: functions, calls, imports, JSX. Everything framework-specific moves into [`wiretap/adapters/`](../../wiretap/adapters/), one file per framework or library, which adds plain records (route, router, mount, table use, HTTP call, job) tagged with its name. The linker works from the records only. Moving the existing rules changed nothing: the Khaoogully map before and after has the same 1,708 nodes and 8,707 links.
- TypeScript apps get their routes collected before any API call is matched, so a client can reach an Express or Next.js backend, and a Next.js page its own routes.
- A coverage report ([`coverage.py`](../../wiretap/coverage.py)) counts, per app, what each adapter found and how many API calls matched, and names code in other languages and dependencies no adapter reads (`NOT_READ`). It is printed by `index` and at server start, and shown in the System view. `index --explain <file>` shows one file's records.
- With no `pathPrefix` configured, a prefix that makes most of an app's unmatched calls match (the part a client's base URL carries, like `/api/v1`) is added, and the report says so.

## Consequences

- A new framework is one adapter file and a fixture ([docs/adapters.md](../adapters.md)); the linker and UI pick it up.
- Adapters see every node independently, so two of them can record the same thing; overlapping ones share a marker (`flask` and `fastapi` share `x.flask_vars`).
- Express-style routes are recognised by shape (a path and a handler function, or an object made by `express()` / `Router()`), which can misread an unusual client call. Next.js routes come from file paths, so a custom `basePath` or rewrites aren't applied.
- The guessed prefix can be wrong when an app calls two backends with different prefixes; setting `pathPrefix` (even to `""`) turns the guess off.
