"""``python -m proxyhub.nodes``: the test-node controls of the dashboard API, for the operator
runbook on the panel server (inside the api or worker container, which hold the database and the
master key). Exactly the same service functions and safety gates as the API; actor ``cli``.

    list                         nodes as GET /api/nodes returns them
    test-node [--location fsn1]  request THE test node (POST /api/nodes/test)
    retire NODE_ID               POST /api/nodes/{id}/retire
    events NODE_ID               GET /api/nodes/{id}/events
    wait NODE_ID STATE [--timeout SECONDS]
                                 poll until the node is in STATE (exit 1 on timeout or deleted)
    servers                      read-only, LIVE from Hetzner: our servers (managed-by=proxy-hub)
                                 in each node account, and how many others there are (rule 4)

Prints JSON only: ids, states and reason codes, never a token or any other secret.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from proxyhub.db.session import get_engine, get_sessionmaker
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.nodes import service

ACTOR = "cli"


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


async def _run(args: argparse.Namespace) -> int:
    sm = get_sessionmaker()
    try:
        if args.cmd == "list":
            async with sm() as db:
                _print(await service.list_nodes(db, datetime.now(UTC)))
            return 0
        if args.cmd == "servers":
            async with sm() as db:
                _print(await service.live_servers(db))
            return 0
        if args.cmd == "test-node":
            async with sm() as db:
                node_id = await service.request_test_node(
                    db, actor=ACTOR, now=datetime.now(UTC), location=args.location
                )
            _print({"node_id": str(node_id)})
            return 0
        if args.cmd == "retire":
            async with sm() as db:
                state = await service.request_retire(db, args.node_id, actor=ACTOR, now=datetime.now(UTC))
            _print({"node_id": str(args.node_id), "state": state})
            return 0
        if args.cmd == "events":
            async with sm() as db:
                _print(await service.node_events(db, args.node_id))
            return 0
        if args.cmd == "wait":
            deadline = time.monotonic() + args.timeout
            seen: str | None = None
            while time.monotonic() < deadline:
                async with sm() as db:
                    nodes = {n["id"]: n for n in await service.list_nodes(db, datetime.now(UTC))}
                node = nodes.get(str(args.node_id))
                seen = node["state"] if node else None
                if seen == args.state:
                    _print({"node_id": str(args.node_id), "state": seen})
                    return 0
                if seen in (None, NodeState.DELETED.value):
                    break
                await asyncio.sleep(5)
            _print({"node_id": str(args.node_id), "state": seen, "error": "not_reached"})
            return 1
    except service.GateRefused as exc:
        _print({"error": exc.code, "reasons": exc.reasons})
        return 1
    except service.NodeServiceError as exc:
        _print({"error": exc.code})
        return 1
    finally:
        await get_engine().dispose()
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m proxyhub.nodes")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("servers")
    test_node = sub.add_parser("test-node")
    test_node.add_argument("--location", default=None)
    for name in ("retire", "events"):
        sub.add_parser(name).add_argument("node_id", type=uuid.UUID)
    wait = sub.add_parser("wait")
    wait.add_argument("node_id", type=uuid.UUID)
    wait.add_argument("state", choices=[s.value for s in NodeState])
    wait.add_argument("--timeout", type=float, default=1500.0)
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
