"""The owner dashboard: serves the built single-page app (``dashboard/dist``) on the panel's own origin.

* ``/assets/*`` are content-hashed files: cached for a year, ``immutable``.
* Any other path that is not a file gets ``index.html`` (client-side routing: /login, /setup,
  /accounts, ...), except paths that look like a file (a dot in the last segment): those are a
  real 404, so a missing script is never answered with HTML.
* It never shadows the API: ``/api``, ``/agent`` and ``/internal`` (and everything below them),
  ``/healthz`` and ``/livez`` do not even match this route, for any method. The match is done by a
  path convertor, so an unknown ``POST /api/x`` is still a plain 404, not a 405 from here.
* Files are only ever read from inside the dashboard directory (no ``..``, no hidden files, no
  symlink escapes).
* HTML responses carry a strict Content-Security-Policy: scripts only from this origin, no
  frames, no third-party anything (fonts are bundled; nothing comes from a CDN).

The directory is ``$PROXYHUB_DASHBOARD_DIR`` if set, else ``/app/dashboard`` (the image), else
``<repo>/dashboard/dist`` (local development). It is looked up per request (two ``stat`` calls), so
a rebuilt dashboard is picked up without a restart.
"""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.convertors import Convertor, register_url_convertor

ENV_DIR = "PROXYHUB_DASHBOARD_DIR"
IMAGE_DIR = Path("/app/dashboard")
DEV_DIR = Path(__file__).resolve().parents[3] / "dashboard" / "dist"

# Path prefixes owned by the API and the node agents, and exact paths of the health endpoints.
RESERVED_PREFIXES = ("api", "agent", "internal")
RESERVED_EXACT = ("healthz", "livez")

CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        # React, Motion and ECharts set inline style attributes; no inline <script> is ever allowed.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "manifest-src 'self'",
        "worker-src 'self' blob:",
    )
)

HTML_HEADERS = {
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
IMMUTABLE = "public, max-age=31536000, immutable"

MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}

NOT_BUILT_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Proxy-Hub</title></head>
<body style="font-family: system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh;">
<p>داشبورد هنوز ساخته نشده است. API پنل کار میکند.</p></body></html>"""


class SpaPathConvertor(Convertor[str]):
    """Matches every path except the reserved API/agent/internal/health paths."""

    regex = (
        r"(?!(?:" + "|".join(RESERVED_PREFIXES) + r")(?:/|$))"
        r"(?!(?:" + "|".join(RESERVED_EXACT) + r")/?$)"
        r".*"
    )

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("proxyhub_spa", SpaPathConvertor())

_RESERVED_RE = re.compile(SpaPathConvertor.regex)


def is_reserved(path: str) -> bool:
    """True for paths (without the leading slash) that belong to the API, never to the dashboard."""
    return _RESERVED_RE.fullmatch(path.lstrip("/")) is None


def dashboard_dir() -> Path | None:
    """The built dashboard directory (it must contain index.html), or None."""
    configured = os.environ.get(ENV_DIR)
    candidates = [Path(configured)] if configured else [IMAGE_DIR, DEV_DIR]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def safe_file(root: Path, rel: str) -> Path | None:
    """``root/rel`` if it is a regular file strictly inside ``root``; never follows ``..``,
    hidden names, backslashes, NUL bytes or symlinks that lead outside."""
    if not rel or "\x00" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    base = root.resolve()
    try:
        target = base.joinpath(*parts).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target


def _media_type(path: Path) -> str:
    known = MEDIA_TYPES.get(path.suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _looks_like_file(rel: str) -> bool:
    return "." in rel.rsplit("/", 1)[-1]


router = APIRouter(include_in_schema=False)


@router.api_route("/{path:proxyhub_spa}", methods=["GET", "HEAD"])
async def dashboard(path: str) -> Response:
    root = dashboard_dir()
    if root is None:
        return HTMLResponse(NOT_BUILT_HTML, status_code=503, headers=HTML_HEADERS)
    if path and path != "index.html":
        target = safe_file(root, path)
        if target is not None:
            headers = {"Cache-Control": IMMUTABLE if path.startswith("assets/") else "no-cache"}
            headers["X-Content-Type-Options"] = "nosniff"
            return FileResponse(target, media_type=_media_type(target), headers=headers)
        if path.startswith("assets/") or _looks_like_file(path):
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(root / "index.html", media_type=MEDIA_TYPES[".html"], headers=HTML_HEADERS)
