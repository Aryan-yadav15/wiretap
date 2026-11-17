# 0004 · Questions are drawn as flows

- **Status:** accepted
- **Date:** 2026-11-17

## Context

Many Claude Code sessions are questions ("can the admin change the delivery partner?"): Claude reads and greps, changes nothing. Drawing every file it touched as one graph produced a tangle where several unrelated flows crossed, and everything appeared at once, with no sense of the order Claude looked in.

## Decision

When a task has no edits and no plan, the Live view switches to rows ("strands"), one per flow Claude followed ([`web/app.js`](../../web/app.js) `buildStrands`, `drawStrands`):

- What Claude looked at is computed per function: line ranges from `sed -n` / `head` / Read offsets, and `grep -n` hits, mapped to the functions on those lines ([`wiretap/session.py:231`](../../wiretap/session.py#L231)). A narrow look counts more than a broad one.
- Each function is traced back to where its flow starts (a tap, a screen opening, a route) and forward to other looked-at functions and the tables it uses.
- Flows that share a start branch off; many small calls between the same two files fold into one row; functions read but not on a flow are grouped per file.
- Boxes are numbered in the order Claude looked, new boxes fade in, and **Replay** rebuilds the view step by step from the timeline.

## Consequences

- A question session reads as a few sentences ("Tap “Proceed” on Payment → createOrder() → POST /orders → …") instead of a graph to decode.
- The ranking is heuristic: a function Claude read inside a long range scores less than one it grepped for, which is usually but not always what mattered.
- Shared resources (database client, thread pool) are left out of this view on purpose; the Resources view has them.
