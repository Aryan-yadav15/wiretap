# 0006 · Plain English on top of the parsed map, never instead of it

- **Status:** accepted
- **Date:** 2026-11-22

## Context

The map names things the way the code does: `handlePartnerSave()`, `admin_orders.py`, a chain of nine hops from a tap to a table. That is exact, but someone who didn't write the code has to read every name to see what a flow does. Tools that have a model draw the whole map read well ("Validates account forms and sends sign-in requests"), but their structure comes from the same model, costs tokens on every rebuild, and can't be checked against anything.

Comments and docstrings would be free, but most code has few, and they go stale.

We want both depths on every canvas: the raw map, and a simple one a newcomer can read, with one switch between them.

## Decision

- **The parser owns the structure; a model only names it.** Simple mode never adds, removes or reroutes a link. Every box in it stands for parsed nodes, and every step of a simplified flow lists the real functions inside it.
- **Descriptions are written per file, bottom up.** A `claude -p --model haiku` call reads one file and returns a short name and a one-line description for the file and for each function it was asked about, only those the parser found. Names it returns for anything else are dropped. A file with more than 20 functions is split into calls that run side by side, because the length of the answer is what takes the time. Up to 8 files are described at once (`WIRETAP_DESCRIBE_WORKERS`), the one in focus first. Higher levels read these lines, not code: a flow is named from its functions' lines.
- **Flow steps come from rules, names from the model.** A flow (the "where it starts" chain) is cut into steps where the layer changes (screen, UI, client, route, logic, data), so consecutive plumbing in one layer collapses into one step. One call names the steps and gives the flow a title. The response must name exactly that many steps, or it is rejected.
- **Cached by hash, described lazily.** Each function's description is stored with the function hash the parser already computes ([0003](0003-function-level-diffs.md)), in `describe.json` next to the extraction cache. A function whose hash changed is described again the next time it is shown; nothing runs on a timer. Only files drawn on the canvas are described, when Simple is on. `python3 -m wiretap describe` fills the cache for a whole project ahead of time.
- **Off by default and marked.** Raw stays the default view, and nothing calls a model until Simple is switched on. Simple text carries an **AI** mark in the side panel, the raw name stays on every box as its second line, and `S` switches back.
- **Same rules as Summarize ([0001](0001-zero-token-hooks.md)).** Calls run with `WIRETAP_SKIP=1`, from the Wiretap folder, so they never show up in Live. Watching Claude stays free.

## Consequences

- Wiretap now spends tokens, but only on request and only once per version of a function. The bottom level dominates the cost: about one call per file shown. Everything above it is a small call over short lines.
- A description can be wrong; the structure can't. When they disagree, the raw name under it and the code in the panel are one click away.
- Describing needs the `claude` CLI signed in. Without it, Simple shows raw names and says why.
- Editing one function asks again about that function only (plus the file's own line); the others keep their names.
- Speed on a real project: about 15 s for a 600-line file, 20 s for a 36-function file split in two. A first look at 30 files should take about a minute with 8 at once (estimated from those, not yet timed end to end); after that it's instant until the code changes.
- Changing the prompt bumps the cache format, so every name is written again once.
- Flow grouping by layer is predictable but blunt: two unrelated helpers in one layer become one step. The step lists its functions, so nothing is hidden.
