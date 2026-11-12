"""Rule 5 in CI: the refusals of the only code that creates or deletes a Hetzner server
(proxyhub/hetzner/mutations.py). Until now only the ``hetzner``-marked tests ran it, and only on
its allowed path.

Nothing imitates Hetzner (D10): a REAL hcloud client points at a local port nothing listens on
(``closed_port_reader``), and servers are hcloud's own ``BoundServer`` built from Hetzner-shaped JSON
(``test_analytics_support.server_json``).
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from typing import Any

import pytest
from test_analytics_support import bound_server, server_json
from test_fleet_live_support import closed_port_reader

from proxyhub.accounts.hetzner_io import HetznerFailure, call_hetzner
from proxyhub.hetzner.client import HetznerReader
from proxyhub.hetzner.mutations import CreateSpec, _still_deletable, create_server, delete_server
from proxyhub.safety.gates import (
    AccountView,
    CreateRequest,
    NodeRowView,
    OperatorLimits,
    PermitError,
    ServerView,
    issue_create_permit,
    issue_delete_permit,
)

SPEC = CreateSpec(
    name="ph-test-00000000",
    server_type="cpx02",
    location="fsn1",
    image="ubuntu-24.04",
    role="node",
    node_id="x",
)


class CountingReader:
    """A real reader (closed port) that counts every touch of its hcloud client."""

    def __init__(self) -> None:
        self.inner = closed_port_reader("0" * 64)
        self.calls = 0

    @property
    def raw(self) -> Any:
        self.calls += 1
        return self.inner.raw


def _reader(r: CountingReader) -> HetznerReader:
    return r  # type: ignore[return-value]


def _create_permit() -> Any:
    limits = OperatorLimits(
        kill_switch=False, budget_cap_eur=Decimal("100"), node_cap_total=10, creations_per_hour_cap=3
    )
    account = AccountView(
        id="a", state="active", guard_ok=True, learned_server_limit=None, servers_in_account=0
    )
    req = CreateRequest(
        account=account,
        fleet_live_total=0,
        creations_last_hour=0,
        projected_month_eur=Decimal("0"),
        new_server_rest_of_month_eur=Decimal("6.49"),
    )
    return issue_create_permit(limits, req)


def _delete_permit() -> Any:
    return issue_delete_permit(
        ServerView(7, "x", {"managed-by": "proxy-hub", "role": "node"}), NodeRowView("a", 7, "retiring"), "a"
    )


def test_no_mutation_without_its_own_permit() -> None:
    """Anything but a permit of the right kind is refused before Hetzner is touched."""
    for call in (
        lambda r: delete_server(r, object()),  # type: ignore[arg-type]
        lambda r: delete_server(r, _create_permit()),
        lambda r: create_server(r, object(), SPEC),  # type: ignore[arg-type]
        lambda r: create_server(r, _delete_permit(), SPEC),
    ):
        reader = CountingReader()
        with pytest.raises(PermitError):
            call(_reader(reader))
        assert reader.calls == 0


async def test_a_delete_reads_the_server_again_before_deleting() -> None:
    """With a genuine permit the server is first read again (here: Hetzner unreachable), never
    deleted on the permit alone."""
    reader = CountingReader()
    with pytest.raises(HetznerFailure) as failure:
        await call_hetzner(lambda: delete_server(_reader(reader), _delete_permit()))
    assert failure.value.kind == "unreachable" and reader.calls == 1  # get_by_id only


@pytest.mark.parametrize(
    ("labels", "protection", "why"),
    [
        ({"role": "node"}, False, "labels changed"),
        ({"managed-by": "someone-else", "role": "node"}, False, "labels changed"),
        ({"managed-by": "proxy-hub", "role": "panel"}, False, "labels changed"),
        ({"managed-by": "proxy-hub"}, False, "labels changed"),
        ({"managed-by": "proxy-hub", "role": "node"}, True, "delete protection"),
    ],
)
def test_a_server_that_changed_since_the_permit_is_not_deleted(
    labels: dict[str, str], protection: bool, why: str
) -> None:
    data = server_json(7, labels=labels)
    data["protection"]["delete"] = protection
    with pytest.raises(PermitError, match=why):
        _still_deletable(bound_server(data))


@pytest.mark.parametrize("role", ["node", "builder"])
def test_our_node_or_builder_server_is_deletable(role: str) -> None:
    _still_deletable(bound_server(server_json(7, labels={"managed-by": "proxy-hub", "role": role})))


def test_delete_server_checks_the_fresh_read_before_it_deletes() -> None:
    """The re-check runs on the server just read, between the read and the delete: a server whose
    labels or protection changed after the permit is never deleted."""
    from proxyhub.hetzner import mutations

    (fn,) = [
        n for n in ast.parse(inspect.getsource(mutations)).body if getattr(n, "name", "") == "delete_server"
    ]
    calls = [
        c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    ]
    order = [c for c in calls if c in ("get_by_id", "_still_deletable", "delete")]
    assert order == ["get_by_id", "_still_deletable", "delete"], calls
