"""Router auto-discovery: every module ``proxyhub.api.routes_<name>`` that defines ``router`` is
included, in alphabetical order. Feature teams add a new ``routes_*.py`` file instead of editing
app.py. A module named ``routes_zz_static`` (the dashboard SPA fallback) is therefore last."""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter, FastAPI

import proxyhub.api as api_pkg


def include_all(app: FastAPI) -> list[str]:
    included: list[str] = []
    for info in sorted(pkgutil.iter_modules(api_pkg.__path__), key=lambda m: m.name):
        if not info.name.startswith("routes_"):
            continue
        module = importlib.import_module(f"proxyhub.api.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router)
            included.append(info.name)
    return included
