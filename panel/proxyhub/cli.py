"""Entry points: ``proxyhub migrate | worker | api``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from alembic.config import Config

from proxyhub.config import get_config


def _alembic_config() -> Config:
    here = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(here / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_config().async_database_url().replace("%", "%%"))
    return cfg


def migrate(revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic_config(), revision)


def downgrade(revision: str) -> None:
    from alembic import command

    command.downgrade(_alembic_config(), revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proxyhub")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("worker")
    api = sub.add_parser("api")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104 - inside the container network only
    api.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=get_config().log_level,
        format='{"t":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}',
    )
    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "worker":
        from proxyhub.worker import run

        asyncio.run(run())
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run("proxyhub.api.app:app", host=args.host, port=args.port, proxy_headers=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
