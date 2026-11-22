"""SQLAlchemy models: class Order(Base): __tablename__ = "orders". A function that names Order uses the orders table."""

import ast

from .pyast import text

NAME, KIND, LABEL = "sqlalchemy", "tables", "SQLAlchemy models"


def module(x, node):
    if isinstance(node, ast.ClassDef):
        for b in node.body:
            if isinstance(b, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in b.targets):
                t = text(b.value)
                if t:
                    x.model_tables[node.name] = t
