# Contributing

Thanks for helping. The most useful contribution is usually **support for a framework or library Wiretap doesn't read yet**: a route style, an ORM, an HTTP client. Each one is a single adapter file.

## Is my stack supported?

Run Wiretap on your project and read what it says it understood:

```bash
python3 -m wiretap index --root path/to/your/project
python3 -m wiretap index --root path/to/your/project --explain path/to/your/project/some/file.ts
```

It lists, per app, the routes, tables, HTTP calls and jobs it found and which adapter found them; code in languages it doesn't parse; and libraries in your dependencies that no adapter reads.

## Adding an adapter

[docs/adapters.md](docs/adapters.md) has the record formats, a Python and a TypeScript example, and how to test. In short:

1. Add `wiretap/adapters/<name>.py` (and list it in `PY_ADAPTERS`) or `wiretap/adapters/ts_<name>.mjs` (loaded automatically).
2. Add the smallest project that uses the framework to `tests/fixture-stacks/`, and a test that checks the routes, tables or links it produces.
3. Take the library off `NOT_READ` in `wiretap/adapters/__init__.py`.
4. Run `python3 -m unittest discover -s tests -v` and `python3 -m wiretap index --root` on a real project that uses it.

## Ground rules

- **No new dependencies.** The server is Python standard library only; the only Node package is TypeScript; the UI's only library is ELK, copied into `web/vendor/elk`.
- **Read code, don't run it** ([decision 0002](docs/decisions/0002-static-parsing.md)). Record what you can read with confidence and leave the rest out. The coverage report makes gaps visible, which is better than a guess on the map.
- **Keep existing maps the same.** If your change moves records around, build a real project before and after and compare nodes and links.
- **Write like the surrounding code**: short comments that say why, plain names, and docs in plain words.

## Reporting a stack that doesn't work

Open an issue with the "Support my stack" template. Include the `index --root` output and the smallest code sample that shows the pattern (a route declaration, a query, a client call). Leave out anything private.
