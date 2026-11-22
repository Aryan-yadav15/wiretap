"""Adapters: the parts of Wiretap that know a framework or library.

The core parsers find functions, calls and imports in any Python or TypeScript file. An adapter recognises one
framework's way of declaring a route, reading a table or making an HTTP call, and adds a plain record for it
(a route, a table use, an HTTP call, ...). The linker in graph.py turns those records into the map, whatever
adapter made them. Every record carries `via`: the adapter's name, which the coverage report counts.

Python adapters are modules in this package; each has NAME, KIND, LABEL and any of these hooks:
    module(x, node)                  every top-level statement of a file
    decorator(x, qual, d, fn)        every decorator on a function or method (`qual` is its name, `fn` its node)
    call(x, qual, node, visit)       every call inside a function (`visit(node)` walks a sub-tree as part of it)
`x` is the file's extraction (wiretap/py_extract.py): add records with x.add("routes", {...}, NAME).

TypeScript adapters are the ts_*.mjs files next to this one (see ts_http.mjs for the shape).
How to write one, with the record formats: docs/adapters.md.
"""

import os

from . import fastapi, flask, http_out, jobs, sqlalchemy, supabase

PY_ADAPTERS = [fastapi, flask, supabase, sqlalchemy, jobs, http_out]

HERE = os.path.dirname(os.path.abspath(__file__))
TS_ADAPTERS = sorted(os.path.join(HERE, f) for f in os.listdir(HERE) if f.startswith("ts_") and f.endswith(".mjs"))

# Frameworks and libraries a project may use that no adapter reads yet: dependency name -> what is missed.
# The coverage report names the ones it finds, so nobody mistakes a silent gap for "nothing there".
NOT_READ = {
    "ts": {
        "@nestjs/core": "NestJS routes", "koa": "Koa routes", "@trpc/server": "tRPC procedures",
        "drizzle-orm": "Drizzle queries", "typeorm": "TypeORM queries", "sequelize": "Sequelize queries",
        "mongoose": "Mongoose queries", "knex": "Knex queries", "kysely": "Kysely queries", "@apollo/server": "GraphQL resolvers", "graphql-yoga": "GraphQL resolvers",
    },
    "py": {
        "django": "Django routes and ORM", "sqlmodel": "SQLModel tables", "tortoise-orm": "Tortoise ORM queries",
        "peewee": "Peewee queries", "pymongo": "MongoDB queries", "motor": "MongoDB queries", "strawberry-graphql": "GraphQL resolvers",
        "graphene": "GraphQL resolvers", "aiohttp": "aiohttp routes",
    },
}

