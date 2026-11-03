"""``python -m proxyhub setup-link --base-url https://panel.example``: one-time owner setup link.

The token sits in the URL fragment (``/setup#<token>``), which browsers never send to a server, so
it cannot end up in any proxy or access log; the setup page posts it in a JSON body. The link works
for 60 minutes, once, and only while no admin account exists. Making a new link kills older ones.
The link is printed to stdout for the person running the command and is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from proxyhub.auth.service import SETUP_TOKEN_TTL, SetupRefusedError, create_setup_token
from proxyhub.config import get_config

log = logging.getLogger("proxyhub.auth")


def normalize_base_url(raw: str) -> str:
    """``https://host[:port][/path]`` without a trailing slash, query or fragment."""
    parts = urlsplit(raw.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("base URL must be an https:// URL with a host name")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("base URL must not contain credentials, a query or a fragment")
    return f"https://{parts.netloc}{parts.path.rstrip('/')}"


def build_link(base_url: str, token: str) -> str:
    return f"{normalize_base_url(base_url)}/setup#{token}"


async def create_link(base_url: str) -> str:
    """Create a token in the database and return the full link. Uses its own short-lived engine."""
    base = normalize_base_url(base_url)
    engine = create_async_engine(get_config().async_database_url(), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            token = await create_setup_token(session)
    finally:
        await engine.dispose()
    log.info("setup link created (valid %d minutes, single use)", SETUP_TOKEN_TTL.total_seconds() // 60)
    return build_link(base, token)


def run(base_url: str) -> int:
    try:
        normalize_base_url(base_url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        link = asyncio.run(create_link(base_url))
    except SetupRefusedError:
        print("error: an admin account already exists; a setup link is refused", file=sys.stderr)
        return 1
    print(link)
    return 0
