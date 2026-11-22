# 0007 · Lay out the Map with ELK, and browse an app a folder at a time

- **Status:** accepted
- **Date:** 2026-11-22

## Context

The Map put boxes in columns chosen ahead of time, by role: screens, components, client logic, API routes, backend logic, data. That reads well when a flow crosses roles (a screen calls a route that writes a table). It falls apart in a backend made of logic files: the file in focus, what uses it and what it reaches all land in one "backend logic" column, and the arrows loop back on themselves. Sorting rows by their neighbours (six passes) couldn't fix that, because the columns were wrong to begin with.

The Map also started at file level. On a 600-file project there was no level between "six apps" and "this one file".

Floorplan, a similar tool, draws a much calmer canvas. Reading its viewer, the main differences were: a layered layout engine (ELK) that works out the columns from the links, right-angle lines, lines whose colour and width say what they carry, boxes that show less as you zoom out, and levels you go into one at a time.

## Decision

- **ELK lays out the Map and the new Folders view.** `elkjs` 0.9.3 is copied, unchanged, into [`web/vendor/elk/`](../../web/vendor/elk/) (EPL-2.0) and runs in a Web Worker. Its layered algorithm puts a caller left of what it calls, in as many columns as the chain needs, keeps lines from crossing, and routes them at right angles. Boxes are held in three bands (uses it · in focus · it reaches; or used by · inside · uses) with ELK's partitioning, so a bracket over each band is always true. **Columns: by role** keeps the old lanes, and is what the Map falls back to if the script didn't load.
- **Lines carry information.** Colour is what a link is (calls, API calls, reads or writes data, renders, outside service), width grows with how many links it stands for, and hovering a line names both ends, what it carries and the routes or labels behind it. Hovering a box fades everything it isn't linked to.
- **Boxes show less as you zoom out.** Below 75% the second line of description goes; below 50% a box shows only its title, in large type. In Simple mode boxes are taller and show the model's sentence under the name ([0006](0006-simplify-layer.md)).
- **Folders: System → app → folder → file.** Double-clicking an app opens its folders. Each level shows what's inside it, what uses it on the left and what it uses on the right. Outside things appear at the nearest level they share with it: a sibling folder, another app, a database, an outside service. A folder that holds only one folder is skipped through (`src/app`). Backspace goes up. Folder levels come from the paths, so they need no model.
- **`vendor` folders aren't mapped**, so copied-in libraries like this one stay off the map.

## Consequences

- The UI has one third-party file. It's served from the Wiretap process like the rest of `web/`, so nothing is fetched from the internet, and there's still no build step. The server serves files under `web/` and its folders only, and only HTML, JS and CSS.
- The 1.6 MB worker loads on first use; after that a 37-box map lays out in well under a second. Layouts are cached per set of boxes and links, so a redraw (a name arriving in Simple mode, a live update) doesn't lay out again.
- Lines are drawn by ELK until a box is dragged; its lines are then redrawn as simple right angles from the new place.
- Folders show structure, not meaning: a folder's name is its path. Simple mode lists the plain names of its busiest files under it, but doesn't name the folder itself.
