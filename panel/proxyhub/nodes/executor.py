"""Node executor: the first REAL node pipeline, for owner-triggered TEST nodes only, fail-closed.

    requested --(gates, live account guard, CreatePermit)--> creating --(Hetzner create)--> booting
    booting --(agent registers with its one-time token)--> registering --(first good report)--> ready
    booting/registering/ready --(timeouts, no heartbeat)--> unhealthy --(good reports)--> ready
    any --(owner retire, or a test node unhealthy for an hour)--> retiring
    retiring --(can_delete on a LIVE read + DeletePermit)--> deleted

Only nodes with ``is_test`` are ever touched here: the 9-slot fleet stays dry-run
(``proxyhub.reconcilers.fleet.FLEET_LIVE_ENABLED`` is False and this module never reads it).

Every step is small and recorded (``node_events``, ``actions_log``); state changes are committed
BEFORE the Hetzner call they lead to, so a crash at any point is recovered on the next pass:
* the server carries the label ``node-id=<id>`` and a name unique in its project, so a create that
  timed out (or a worker that died mid-call) is found again by label and never made twice;
* a node whose create never produced a server is closed only after a lookup grace period that
  outlasts the slowest possible create call.

The user data (which holds the one-time registration token) lives only in memory between rendering
and the create call; it is never logged, stored or put into an error (CLAUDE.md rule 3).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hcloud import APIException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from proxyhub.accounts.hetzner_io import HetznerFailure, ServersObservation, call_hetzner, observe_servers
from proxyhub.accounts.limits import record_resource_limit
from proxyhub.agent_api.tokens import issue_registration_token
from proxyhub.config import get_config
from proxyhub.db.models import HetznerAccount, Node
from proxyhub.db.models_agent import AgentNode, AgentNodeReport, TelemtNodeConfig
from proxyhub.db.models_fleet import AccountServer
from proxyhub.db.models_live import NodeOperation
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.hetzner.client import HetznerReader
from proxyhub.hetzner.mutations import CreateSpec, create_server, delete_server
from proxyhub.nodes.cloud_init import UserData, render_user_data
from proxyhub.nodes.image import NodeImage, load_node_image
from proxyhub.nodes.service import (
    LiveAccountLook,
    account_reader,
    evaluate_create,
    live_server,
    load_settings,
)
from proxyhub.nodes.settings import get_live_settings
from proxyhub.nodes.transitions import ensure_operation, event, lock_node, transition
from proxyhub.pki.ca import build_ca_certificate, load_ca_key, to_pem
from proxyhub.reconcilers.account_guard import AccountRef, apply_failure, apply_observation
from proxyhub.safety.gates import (
    MANAGED_LABEL,
    MANAGED_VALUE,
    NodeRowView,
    can_delete,
    issue_create_permit,
    issue_delete_permit,
)
from proxyhub.vault import Vault

log = logging.getLogger(__name__)

ACTOR = "node_executor"
S = NodeState

# A requested node whose live account look keeps failing (network, rate limit) is given up after this.
REQUEST_RETRY_WINDOW = timedelta(minutes=10)
# A create call may still finish after call_hetzner gave up waiting (hcloud retries a timed-out
# request up to 5 times: about 6 x 40 s + 31 s backoff). Lookups by label continue this long.
CREATE_LOOKUP_GRACE = timedelta(minutes=10)
# Ubuntu boot + apt + pinned downloads + provisioning: a few minutes on a CPX02; generous bound.
BOOT_TIMEOUT = timedelta(minutes=20)
# From registration to the first report showing the served desired state applied.
REGISTER_TIMEOUT = timedelta(minutes=10)
# The agent polls every 15 s (last_seen_at moves at most every 30 s) and reports every 60 s.
HEARTBEAT_TIMEOUT = timedelta(minutes=5)
# A test node that stays unhealthy this long is retired (through the delete gate) to stop its cost.
UNHEALTHY_TEST_RETIRE_AFTER = timedelta(hours=1)
TRANSIENT_FAILURES = frozenset({"unreachable", "rate_limited"})
TRANSIENT_CODES = frozenset(
    {"timeout", "conflict", "bad_gateway", "service_unavailable", "500", "502", "503"}
)
LIVE_STEP_STATES = (S.REQUESTED, S.CREATING, S.BOOTING, S.REGISTERING, S.READY, S.UNHEALTHY, S.RETIRING)


class FleetNotLive(PermissionError):
    """Raised if the executor is ever handed a non-test node."""


def default_ca_pem() -> str:
    return to_pem(build_ca_certificate(load_ca_key(get_config().agent_ca_key_file)))


@dataclass(frozen=True)
class NodeRef:
    id: uuid.UUID
    name: str
    account_id: uuid.UUID
    location: str
    server_type: str
    state: NodeState
    is_test: bool
    hetzner_id: int | None
    ipv4: str | None


@dataclass(frozen=True)
class AgentHealth:
    registered: bool
    heartbeat_fresh: bool
    # A recent report since registration: the local apply worked and Telemt runs where it must.
    working: bool
    # ... and it applied exactly the desired state the panel served last (needed to become ready;
    # right after the panel changes the desired state it lags one poll + report, which is no fault).
    current: bool

    @property
    def good_report(self) -> bool:
        return self.working and self.current


def _transient(failure: HetznerFailure) -> bool:
    return failure.kind in TRANSIENT_FAILURES or failure.code in TRANSIENT_CODES


def _public_ips(server: object) -> tuple[str | None, str | None]:
    net = getattr(server, "public_net", None)
    v4 = getattr(getattr(net, "ipv4", None), "ip", None)
    v6 = getattr(getattr(net, "ipv6", None), "ip", None)
    return (str(v4) if v4 else None, str(v6) if v6 else None)


class NodeExecutor:
    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        reader_factory: Callable[[str], HetznerReader] = HetznerReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ca_pem: Callable[[], str] = default_ca_pem,
        node_image: Callable[[], NodeImage] = load_node_image,
    ) -> None:
        self._vault_factory = vault_factory
        self._reader_factory = reader_factory
        self._clock = clock
        self._ca_pem = ca_pem
        self._node_image = node_image

    # --- driver ---------------------------------------------------------------------------------

    async def run_once(self, engine: AsyncEngine) -> None:
        """One pass over every live test node (the caller holds the executor's advisory lock)."""
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            ids = (
                (
                    await s.execute(
                        select(Node.id)
                        .where(Node.is_test.is_(True), Node.state.in_([st.value for st in LIVE_STEP_STATES]))
                        .order_by(Node.created_at, Node.id)
                    )
                )
                .scalars()
                .all()
            )
        for node_id in ids:
            try:
                await self.step(sm, node_id)
            except Exception as exc:  # one node's trouble never stops the others
                log.warning("node executor: %s on node %s", type(exc).__name__, node_id)
                await self._note_error(sm, node_id, f"internal:{type(exc).__name__}")

    async def step(self, sm: async_sessionmaker[AsyncSession], node_id: uuid.UUID) -> None:
        node = await self._load(sm, node_id)
        if node is None:
            return
        if not node.is_test:
            raise FleetNotLive("the executor handles test nodes only; the fleet is dry-run")
        handler = {
            S.REQUESTED: self._requested,
            S.CREATING: self._creating,
            S.BOOTING: self._booting,
            S.REGISTERING: self._registering,
            S.READY: self._ready,
            S.UNHEALTHY: self._unhealthy,
            S.RETIRING: self._retiring,
        }.get(node.state)
        if handler is not None:
            await handler(sm, node)

    async def _load(self, sm: async_sessionmaker[AsyncSession], node_id: uuid.UUID) -> NodeRef | None:
        async with sm() as s:
            n = await s.get(Node, node_id)
            if n is None:
                return None
            return NodeRef(
                id=n.id,
                name=n.name,
                account_id=n.account_id,
                location=n.location,
                server_type=n.server_type,
                state=S(n.state),
                is_test=n.is_test,
                hetzner_id=n.hetzner_id,
                ipv4=str(n.ipv4) if n.ipv4 is not None else None,
            )

    async def _op(self, s: AsyncSession, node: Node) -> NodeOperation:
        return await ensure_operation(s, node, self._clock())

    async def _note_error(self, sm: async_sessionmaker[AsyncSession], node_id: uuid.UUID, code: str) -> None:
        try:
            async with sm() as s:
                row = await lock_node(s, node_id)
                if row is None:
                    return
                op = await self._op(s, row)
                if op.last_error != code[:60]:
                    event(s, node_id, self._clock(), "executor_error", code=code[:60])
                op.last_error, op.updated_at = code[:60], self._clock()
                await s.commit()
        except Exception as exc:  # recording must never raise out of the error path
            log.warning("node executor: could not record an error (%s)", type(exc).__name__)

    async def _reader(self, s: AsyncSession, account_id: uuid.UUID) -> HetznerReader | None:
        return await account_reader(s, self._vault_factory(), account_id, self._reader_factory)

    async def _account_ref(self, s: AsyncSession, account_id: uuid.UUID) -> AccountRef | None:
        row = (
            await s.execute(
                select(HetznerAccount.id, HetznerAccount.name, HetznerAccount.state).where(
                    HetznerAccount.id == account_id
                )
            )
        ).first()
        return None if row is None else AccountRef(row.id, row.name, row.state, None)

    # --- requested -> creating -> booting ---------------------------------------------------------

    async def _close_request(
        self,
        s: AsyncSession,
        node_id: uuid.UUID,
        reasons: list[str] | tuple[str, ...],
        context: dict[str, object],
    ) -> None:
        """Abandon a request that may not proceed (no Hetzner call was made): requested -> deleted."""
        now = self._clock()
        row = await lock_node(s, node_id)
        if row is None or row.state != S.REQUESTED.value:
            await s.rollback()
            return
        op = await self._op(s, row)
        op.last_error = (reasons[0] if reasons else "refused")[:60]
        event(s, node_id, now, "create_refused", reasons=list(reasons))
        await transition(s, row, S.DELETED, now, reason="create_refused")
        await log_action(
            s,
            actor=ACTOR,
            action="node_create",
            target=row.name,
            dry_run=False,
            allowed=False,
            reasons=reasons,
            context={"node_id": str(node_id), **context},
            result="refused by the safety gates; no server was created",
        )
        await s.commit()

    async def _requested(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        now = self._clock()
        async with sm() as s:
            other = await self._blocked_by_other(s, node.id)
            if other is not None:  # at most one live test node
                await self._close_request(s, node.id, ["test_node_exists"], {"other_node_id": str(other)})
                return
            ev = await evaluate_create(
                s,
                now,
                location=node.location,
                account_id=node.account_id,
                server_type=node.server_type,
                exclude_node=node.id,
            )
            if not ev.allowed:
                await self._close_request(s, node.id, ev.reasons, ev.context)
                return
            reader = await self._reader(s, node.account_id)
            account = await self._account_ref(s, node.account_id)
            if reader is None or account is None:
                await self._close_request(s, node.id, ["token_missing"], ev.context)
                return
            op_row = await s.get(NodeOperation, node.id)
            requested_at = op_row.requested_at if op_row is not None else now
            await s.rollback()

        # The account guard (CLAUDE.md rule 4), live: ALL servers of the project, no label filter.
        try:
            obs = await call_hetzner(lambda: observe_servers(reader))
        except HetznerFailure as failure:
            async with sm() as s:
                await apply_failure(await s.connection(), account, failure, now)
                if _transient(failure) and now - requested_at < REQUEST_RETRY_WINDOW:
                    row = await lock_node(s, node.id)
                    if row is not None:
                        (await self._op(s, row)).last_error = f"hetzner_{failure.kind}:{failure.code}"[:60]
                    await s.commit()
                    return
                await s.commit()
            async with sm() as s:
                await self._close_request(s, node.id, [f"hetzner_{failure.kind}"], {"code": failure.code})
            return

        async with sm() as s:
            guard_ok = await apply_observation(await s.connection(), account, obs, now)
            await s.commit()
        if not guard_ok:
            async with sm() as s:
                await self._close_request(s, node.id, ["account_guard_failed"], {})
            return

        ours = [srv for srv in obs.managed if srv.labels.get("node-id") == str(node.id)]
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.REQUESTED.value:
                await s.rollback()
                return  # withdrawn by the owner meanwhile
            if ours:  # a server for this node already exists (crash right after a create): adopt it
                await transition(s, row, S.CREATING, now, reason="server_found_by_label")
                row.hetzner_id = ours[0].hetzner_id
                (await self._op(s, row)).server_seen_at = now
                event(s, node.id, now, "server_adopted", hetzner_id=ours[0].hetzner_id)
                await transition(s, row, S.BOOTING, now, reason="server_found_by_label")
                await s.commit()
                return
            ev = await evaluate_create(
                s,
                now,
                location=node.location,
                account_id=node.account_id,
                server_type=node.server_type,
                exclude_node=node.id,
                live=LiveAccountLook(guard_ok=obs.guard.allowed, servers_total=obs.total),
            )
            if not ev.allowed:
                await self._close_request(s, node.id, ev.reasons, ev.context)
                return
            permit = issue_create_permit(ev.limits, ev.request)  # the gate decides once more
            token = await issue_registration_token(s, node.id, now=now)
            try:
                # Rendered inside the transaction: if it fails, the token is never committed.
                user_data = self._render(node, token)
            except Exception as exc:
                await s.rollback()
                await self._close_request(s, node.id, ["user_data_failed"], {"error": type(exc).__name__})
                return
            del token
            op = await self._op(s, row)
            op.create_attempts += 1
            op.create_attempted_at = now  # from here on a server may exist: lookups before closing
            op.last_error = None
            await transition(s, row, S.CREATING, now, reason="gates_passed")
            event(
                s,
                node.id,
                now,
                "create_started",
                location=node.location,
                server_type=node.server_type,
                account_id=str(node.account_id),
                image=get_live_settings().node_os_image,
                user_data_bytes=user_data.size,
            )
            await log_action(
                s,
                actor=ACTOR,
                action="node_create",
                target=node.name,
                dry_run=False,
                allowed=True,
                reasons=[],
                context={"node_id": str(node.id), **ev.context},
                result="creating a test server through CreatePermit",
            )
            await s.commit()

        # The create call happens with no transaction open. The user data is handed to Hetzner
        # and dropped; nothing here logs it.
        spec = CreateSpec(
            name=node.name,
            server_type=node.server_type,
            location=node.location,
            image=get_live_settings().node_os_image,
            role="node",
            node_id=str(node.id),
            user_data=user_data.text,
            is_test=True,
        )
        try:
            hetzner_id = await call_hetzner(lambda: create_server(reader, permit, spec))
        except HetznerFailure as failure:
            await self._create_failed(sm, node, failure, obs)
            return

        ipv4: str | None = None
        ipv6: str | None = None
        try:
            server = await call_hetzner(lambda: reader.raw.servers.get_by_id(hetzner_id))
            ipv4, ipv6 = _public_ips(server)
        except HetznerFailure:
            pass  # filled in while booting
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None:
                return
            row.hetzner_id = hetzner_id
            row.ipv4, row.ipv6 = ipv4, ipv6
            (await self._op(s, row)).server_seen_at = self._clock()
            event(s, node.id, self._clock(), "server_created", hetzner_id=hetzner_id, ipv4=ipv4)
            if row.state == S.CREATING.value:  # else the owner retired it meanwhile: delete next pass
                await transition(s, row, S.BOOTING, self._clock(), reason="server_created")
            await s.commit()

    async def _blocked_by_other(self, s: AsyncSession, me: uuid.UUID) -> uuid.UUID | None:
        """Another live test node that goes first: one already past ``requested``, or an older request."""
        mine = (await s.execute(select(Node.created_at).where(Node.id == me))).scalar_one()
        others = (
            await s.execute(
                select(Node.id, Node.state, Node.created_at).where(
                    Node.is_test.is_(True), Node.state != S.DELETED.value, Node.id != me
                )
            )
        ).all()
        for other in others:
            if other.state != S.REQUESTED.value or (other.created_at, str(other.id)) < (mine, str(me)):
                return uuid.UUID(str(other.id))
        return None

    def _render(self, node: NodeRef, token: str) -> UserData:
        domain = get_live_settings().checked_agent_domain()
        if domain is None:
            raise ValueError("agent domain not set")
        return render_user_data(
            self._node_image(), node_id=node.id, agent_domain=domain, token=token, ca_pem=self._ca_pem()
        )

    async def _create_failed(
        self,
        sm: async_sessionmaker[AsyncSession],
        node: NodeRef,
        failure: HetznerFailure,
        obs: ServersObservation,
    ) -> None:
        now = self._clock()
        transient = _transient(failure)
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None:
                return
            op = await self._op(s, row)
            op.last_error = f"create:{failure.code}"[:60]
            event(s, node.id, now, "create_failed", code=failure.code, transient=transient)
            cause = failure.__cause__
            if isinstance(cause, APIException):  # resource_limit_exceeded teaches the account limit (H7)
                await record_resource_limit(s, node.account_id, obs.total, cause, actor=ACTOR)
            await log_action(
                s,
                actor=ACTOR,
                action="node_create_failed",
                target=node.name,
                dry_run=False,
                allowed=True,
                reasons=[failure.code],
                context={"node_id": str(node.id), "kind": failure.kind, "transient": transient},
                result="Hetzner did not confirm the create",
            )
            # A transient failure may still have created the server: stay "creating" and look it up
            # by label. Otherwise retire: the retiring step looks once more before closing the node.
            if not transient and row.state == S.CREATING.value:
                await self._to_retiring(s, row, now, "create_failed")
            await s.commit()

    async def _to_retiring(self, s: AsyncSession, row: Node, now: datetime, reason: str) -> None:
        await transition(s, row, S.RETIRING, now, reason=reason)
        op = await self._op(s, row)
        op.retire_requested_at, op.retire_reason = now, reason

    async def _servers_by_label(self, reader: HetznerReader, node_id: uuid.UUID) -> list[object]:
        selector = f"{MANAGED_LABEL}={MANAGED_VALUE},node-id={node_id}"
        return list(await call_hetzner(lambda: reader.raw.servers.get_all(label_selector=selector)))

    async def _creating(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        now = self._clock()
        async with sm() as s:
            reader = await self._reader(s, node.account_id)
            row = await s.get(Node, node.id)
            op = await self._op(s, row) if row is not None else None
            attempted = op.create_attempted_at if op is not None else None
            await s.commit()
        if reader is None:
            await self._note_error(sm, node.id, "token_missing")
            return
        try:
            found = await self._servers_by_label(reader, node.id)
        except HetznerFailure as failure:
            await self._note_error(sm, node.id, f"lookup:{failure.code}")
            return
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.CREATING.value:
                await s.rollback()
                return
            if found:
                server = found[0]
                row.hetzner_id = int(getattr(server, "id", 0) or 0)
                row.ipv4, row.ipv6 = _public_ips(server)
                (await self._op(s, row)).server_seen_at = now
                event(s, node.id, now, "server_adopted", hetzner_id=row.hetzner_id, found=len(found))
                await transition(s, row, S.BOOTING, now, reason="server_found_by_label")
            elif attempted is None or now - attempted > CREATE_LOOKUP_GRACE:
                event(s, node.id, now, "no_server_found")
                await self._to_retiring(s, row, now, "create_failed")
            await s.commit()

    # --- booting -> registering -> ready, timeouts ------------------------------------------------

    async def _health(self, s: AsyncSession, node_id: uuid.UUID, now: datetime) -> AgentHealth:
        agent = await s.get(AgentNode, node_id, populate_existing=True)
        if agent is None:
            return AgentHealth(registered=False, heartbeat_fresh=False, working=False, current=False)
        fresh = agent.last_seen_at is not None and agent.last_seen_at >= now - HEARTBEAT_TIMEOUT
        working = (
            agent.last_report_at is not None
            and agent.last_report_at >= agent.registered_at
            and agent.last_report_at >= now - HEARTBEAT_TIMEOUT
            and agent.last_apply_ok is True
        )
        if working and await s.get(TelemtNodeConfig, node_id) is not None:
            telemt_up = (
                await s.execute(
                    select(AgentNodeReport.telemt_up)
                    .where(AgentNodeReport.node_id == node_id)
                    .order_by(AgentNodeReport.at.desc(), AgentNodeReport.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            working = telemt_up is True  # a node that must run Telemt works only with Telemt up
        current = agent.last_applied_etag is not None and agent.last_applied_etag == agent.last_served_etag
        return AgentHealth(registered=True, heartbeat_fresh=fresh, working=working, current=current)

    async def _since(self, s: AsyncSession, row: Node) -> datetime:
        return (await self._op(s, row)).state_since

    async def _booting(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        if node.ipv4 is None and node.hetzner_id is not None:
            await self._refresh_ips(sm, node)
        now = self._clock()
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.BOOTING.value:
                await s.rollback()
                return
            health = await self._health(s, node.id, now)
            if health.registered:
                await transition(s, row, S.REGISTERING, now, reason="agent_registered")
            elif now - await self._since(s, row) > BOOT_TIMEOUT:
                await transition(s, row, S.UNHEALTHY, now, reason="boot_timeout")
            await s.commit()

    async def _refresh_ips(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        hetzner_id = node.hetzner_id
        async with sm() as s:
            reader = await self._reader(s, node.account_id)
            await s.rollback()
        if reader is None or hetzner_id is None:
            return
        try:
            server = await call_hetzner(lambda: reader.raw.servers.get_by_id(hetzner_id))
        except HetznerFailure:
            return
        ipv4, ipv6 = _public_ips(server)
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is not None and row.hetzner_id == hetzner_id and ipv4:
                row.ipv4, row.ipv6 = ipv4, ipv6
            await s.commit()

    async def _registering(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        now = self._clock()
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.REGISTERING.value:
                await s.rollback()
                return
            health = await self._health(s, node.id, now)
            if health.good_report and health.heartbeat_fresh:
                await transition(s, row, S.READY, now, reason="agent_reported")
                await log_action(
                    s,
                    actor=ACTOR,
                    action="node_ready",
                    target=row.name,
                    dry_run=False,
                    allowed=True,
                    reasons=[],
                    context={"node_id": str(node.id), "hetzner_id": row.hetzner_id},
                    result="node ready: agent registered over mTLS and applied its desired state",
                )
            elif now - await self._since(s, row) > REGISTER_TIMEOUT:
                await transition(s, row, S.UNHEALTHY, now, reason="no_good_report")
            await s.commit()

    async def _ready(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        now = self._clock()
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.READY.value:
                await s.rollback()
                return
            health = await self._health(s, node.id, now)
            if not health.heartbeat_fresh:
                await transition(s, row, S.UNHEALTHY, now, reason="heartbeat_lost")
            elif not health.working:
                await transition(s, row, S.UNHEALTHY, now, reason="report_not_good")
            await s.commit()

    async def _unhealthy(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        now = self._clock()
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.UNHEALTHY.value:
                await s.rollback()
                return
            health = await self._health(s, node.id, now)
            if health.good_report and health.heartbeat_fresh:
                await transition(s, row, S.READY, now, reason="agent_recovered")
            elif row.is_test and now - await self._since(s, row) > UNHEALTHY_TEST_RETIRE_AFTER:
                await self._to_retiring(s, row, now, "unhealthy_test_node")
                await log_action(
                    s,
                    actor=ACTOR,
                    action="node_retire_request",
                    target=row.name,
                    dry_run=False,
                    allowed=True,
                    reasons=["unhealthy_test_node"],
                    context={"node_id": str(node.id), "hetzner_id": row.hetzner_id},
                    result="test node unhealthy for an hour: retiring it (delete gate decides)",
                )
            await s.commit()

    # --- retiring -> deleted ------------------------------------------------------------------------

    async def _close_without_server(self, s: AsyncSession, row: Node, now: datetime, reason: str) -> None:
        await transition(s, row, S.DELETED, now, reason=reason)
        await log_action(
            s,
            actor=ACTOR,
            action="node_delete",
            target=row.name,
            dry_run=False,
            allowed=True,
            reasons=[reason],
            context={"node_id": str(row.id), "hetzner_id": row.hetzner_id},
            result="closed without a Hetzner call: no server of this node exists",
        )

    async def _retiring(self, sm: async_sessionmaker[AsyncSession], node: NodeRef) -> None:
        now = self._clock()
        async with sm() as s:
            settings, problems = await load_settings(s)
            row = await s.get(Node, node.id)
            op = await self._op(s, row) if row is not None else None
            attempted = op.create_attempted_at if op is not None else None
            # The Hetzner client is built only when a Hetzner call is needed.
            needs_hetzner = node.hetzner_id is not None or attempted is not None
            reader = await self._reader(s, node.account_id) if needs_hetzner else None
            await s.commit()
        blocked = [*problems, *(["kill_switch_on"] if settings.kill_switch else [])]

        if node.hetzner_id is None:
            if attempted is None:  # no create call was ever made: nothing can exist on Hetzner
                async with sm() as s:
                    row = await lock_node(s, node.id)
                    if row is not None and row.state == S.RETIRING.value:
                        await self._close_without_server(s, row, now, "no_server_requested")
                    await s.commit()
                return
            if reader is None:
                await self._note_error(sm, node.id, "token_missing")
                return
            try:
                found = await self._servers_by_label(reader, node.id)
            except HetznerFailure as failure:
                await self._note_error(sm, node.id, f"lookup:{failure.code}")
                return
            async with sm() as s:
                row = await lock_node(s, node.id)
                if row is None or row.state != S.RETIRING.value or row.hetzner_id is not None:
                    await s.rollback()
                    return
                if found:  # adopt it; the next pass deletes it through the gate
                    row.hetzner_id = int(getattr(found[0], "id", 0) or 0)
                    event(s, node.id, now, "server_adopted", hetzner_id=row.hetzner_id, found=len(found))
                elif now - attempted > CREATE_LOOKUP_GRACE:
                    event(s, node.id, now, "no_server_found")
                    await self._close_without_server(s, row, now, "no_server_found")
                await s.commit()
            return

        if blocked:
            await self._delete_refused(sm, node, blocked)
            return
        if reader is None:
            await self._note_error(sm, node.id, "token_missing")
            return
        hetzner_id = node.hetzner_id
        try:
            server = await live_server(reader, hetzner_id)
        except HetznerFailure as failure:
            await self._note_error(sm, node.id, f"read:{failure.code}")
            return
        if server is None:  # Hetzner no longer has it
            async with sm() as s:
                row = await lock_node(s, node.id)
                if row is not None and row.state == S.RETIRING.value and row.hetzner_id == hetzner_id:
                    event(s, node.id, now, "server_already_gone", hetzner_id=hetzner_id)
                    await self._close_without_server(s, row, now, "server_already_gone")
                    await self._forget_server(s, row.account_id, hetzner_id)
                await s.commit()
            return

        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.RETIRING.value or row.hetzner_id != hetzner_id:
                await s.rollback()
                return
            db_row = NodeRowView(str(row.account_id), row.hetzner_id, row.state)
            decision = can_delete(server, db_row, str(row.account_id))
            if not decision.allowed:
                await s.rollback()
                await self._delete_refused(sm, node, list(decision.reasons))
                return
            permit = issue_delete_permit(server, db_row, str(row.account_id))
            op = await self._op(s, row)
            op.delete_attempts += 1
            event(s, node.id, now, "delete_started", hetzner_id=hetzner_id)
            await s.commit()

        try:
            await call_hetzner(lambda: delete_server(reader, permit))
        except HetznerFailure as failure:
            await self._note_error(sm, node.id, f"delete:{failure.code}")
            return
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None or row.state != S.RETIRING.value:
                await s.rollback()
                return
            done = self._clock()
            event(s, node.id, done, "server_deleted", hetzner_id=hetzner_id)
            await transition(s, row, S.DELETED, done, reason="server_deleted")
            await self._forget_server(s, row.account_id, hetzner_id)
            (await self._op(s, row)).last_error = None
            await log_action(
                s,
                actor=ACTOR,
                action="node_delete",
                target=row.name,
                dry_run=False,
                allowed=True,
                reasons=[],
                context={
                    "node_id": str(node.id),
                    "hetzner_id": hetzner_id,
                    "account_id": str(row.account_id),
                },
                result="server deleted through DeletePermit",
            )
            await s.commit()

    async def _delete_refused(
        self, sm: async_sessionmaker[AsyncSession], node: NodeRef, reasons: list[str]
    ) -> None:
        code = ("delete_refused:" + ",".join(reasons))[:60]
        async with sm() as s:
            row = await lock_node(s, node.id)
            if row is None:
                return
            op = await self._op(s, row)
            if op.last_error != code:  # record a refusal once, not every pass
                event(s, node.id, self._clock(), "delete_refused", reasons=reasons)
                await log_action(
                    s,
                    actor=ACTOR,
                    action="node_delete",
                    target=row.name,
                    dry_run=False,
                    allowed=False,
                    reasons=reasons,
                    context={"node_id": str(node.id), "hetzner_id": row.hetzner_id},
                    result="delete refused by the gate; the server stays until the reasons clear",
                )
            op.last_error = code
            await s.commit()

    @staticmethod
    async def _forget_server(s: AsyncSession, account_id: uuid.UUID, hetzner_id: int | None) -> None:
        """Drop the deleted server from the last account observation (the guard refreshes it anyway)."""
        if hetzner_id is None:
            return
        await s.execute(
            delete(AccountServer).where(
                AccountServer.account_id == account_id, AccountServer.hetzner_id == hetzner_id
            )
        )
