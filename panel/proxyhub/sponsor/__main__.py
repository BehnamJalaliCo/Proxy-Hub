"""``python -m proxyhub.sponsor``: @MTProxybot's own list, for the operator runbook on the panel
server (inside the api or worker container, which hold the database). The same service functions
and checks as the dashboard; actor ``cli``. The sponsor loop (worker) does the talking.

    status                    the list as last read, each proxy's status and sponsor, the job,
                              the latest observation and the screens' shapes (no secret)
    inventory                 queue a read of the whole list and of each live proxy's sponsor
    cleanup                   queue «پاک کردن پروکسی‌های مرده»: "observe" until removal is on
    stop                      end the open job
    removal on|off            real removal (ON only after an observe run saw exactly one
                              removal button of the known shape on a dead proxy's screen)
    wait [--timeout SECONDS]  poll until the open job ended (exit 1 on timeout)

Prints JSON only: addresses, states, reason codes and shapes; never a secret or a tag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from proxyhub.db.session import get_engine
from proxyhub.sponsor import inventory, service
from proxyhub.sponsor.inventory import InventoryError
from proxyhub.sponsor.messages import error_message

ACTOR = "cli"


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
    sys.stdout.flush()


async def _wanted() -> str:
    view = await service.overview(get_engine(), now=datetime.now(UTC), provider_ready=True)
    return str(view["wanted_channel"])


async def _run(args: argparse.Namespace) -> int:
    engine = get_engine()
    now = datetime.now(UTC)
    try:
        if args.cmd == "status":
            _print(await inventory.status(engine, wanted=await _wanted()))
        elif args.cmd in (inventory.INVENTORY, inventory.CLEANUP):
            queued = await inventory.request_job(engine, args.cmd, actor=ACTOR, now=now)
            _print({"job": queued.to_json()})
        elif args.cmd == "stop":
            await inventory.stop_job(engine, actor=ACTOR, now=now)
            _print({"stopped": True})
        elif args.cmd == "removal":
            state = await inventory.set_removal(engine, args.value == "on", actor=ACTOR, now=now)
            _print({"removal_enabled": state.removal_enabled, "observation": state.observation})
        elif args.cmd == "wait":
            deadline = time.monotonic() + args.timeout
            while True:
                async with engine.connect() as db:
                    job = (await inventory.load_state(db)).job
                if job is None or not job.open:
                    _print({"job": job.to_json() if job is not None else None})
                    return 0
                if time.monotonic() >= deadline:
                    _print({"timeout": True, "job": job.to_json()})
                    return 1
                await asyncio.sleep(10)
    except InventoryError as exc:
        _print({"error": exc.code, "message_fa": error_message(exc.code)})
        return 2
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m proxyhub.sponsor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser(inventory.INVENTORY)
    sub.add_parser(inventory.CLEANUP)
    sub.add_parser("stop")
    removal = sub.add_parser("removal")
    removal.add_argument("value", choices=["on", "off"])
    wait = sub.add_parser("wait")
    wait.add_argument("--timeout", type=float, default=3600.0)
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
