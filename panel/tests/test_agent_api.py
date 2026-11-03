"""Agent API end to end through the real app (ASGI) on a real PostgreSQL with real cryptography.

Covers one-time registration, the proxy-secret + client-certificate trust rule, desired state with
ETag, renewal and supersession, strict reports without any IP, and that no token or secret ever
reaches a log line, a response it does not belong in, or the database in clear.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from test_agent_support import (
    FP_HEADER,
    PROXY_HEADER,
    SecretFiles,
    clean_agent_tables,
    create_node,
    create_registration,
    reset_config_caches,
    use_secret_files,
    write_secret_files,
)

from proxyhub.agent_api.desired import (
    DesiredStateError,
    create_telemt_user,
    reveal_secret,
    set_telemt_node_config,
    set_user_sponsor_tag,
)
from proxyhub.agent_api.sealing import SealError, load_seal_key, open_secret, seal_secret
from proxyhub.agent_api.tokens import TokenRefused, issue_registration_token, token_digest
from proxyhub.agent_api.validation import looks_like_ip, valid_domain
from proxyhub.db.session import get_engine
from proxyhub.pki.ca import build_ca_certificate, load_ca_key, to_pem

pytestmark = pytest.mark.db

TAG_A = "a" * 32
TAG_B = "b" * 32


@dataclass
class Env:
    engine: AsyncEngine
    files: SecretFiles
    client: AsyncClient

    def maker(self) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(self.engine, expire_on_commit=False)

    def headers(self, fingerprint: str | None = None, secret: str | None = None) -> dict[str, str]:
        h = {PROXY_HEADER: self.files.proxy_secret if secret is None else secret}
        if fingerprint is not None:
            h[FP_HEADER] = fingerprint
        return h

    async def token(self, node_id: uuid.UUID, now: datetime | None = None) -> str:
        async with self.maker()() as session:
            token = await issue_registration_token(session, node_id, now=now)
            await session.commit()
            return token

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []

    @property
    def seal_key(self) -> bytes:
        return load_seal_key(self.files.master_key)


@pytest.fixture
async def env(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    from proxyhub.api.app import app

    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    engine = create_async_engine(migrated_db)
    await clean_agent_tables(engine)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://agent.test") as client:
            yield Env(engine=engine, files=files, client=client)
    finally:
        await engine.dispose()
        await get_engine().dispose()
        reset_config_caches()


def make_csr(node_id: uuid.UUID) -> tuple[ec.EllipticCurvePrivateKey, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(node_id))]))
        .sign(key, hashes.SHA256())
    )
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def fingerprint(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def register(env: Env, node_id: uuid.UUID, token: str | None = None) -> Response:
    _, csr = make_csr(node_id)
    body = {
        "token": token or await env.token(node_id),
        "csr_pem": csr,
        "agent_version": "0.1.0",
        "telemt_version": "3.5.16",
    }
    return await env.client.post("/agent/v1/register", json=body, headers=env.headers())


async def registered(env: Env, state: str = "booting") -> tuple[uuid.UUID, str]:
    node_id = await create_node(env.engine, state)
    resp = await register(env, node_id)
    assert resp.status_code == 200, resp.text
    return node_id, fingerprint(resp.json()["cert_pem"])


def report_body(**override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": 1,
        "agent_version": "0.1.0",
        "applied_etag": '"' + "c" * 64 + '"',
        "apply_ok": True,
        "apply_error": None,
        "telemt": {
            "up": True,
            "version": "3.5.16",
            "uptime_s": 3725.4,
            "connections_total": 1532,
            "connections_bad_total": 17,
            "handshake_timeouts_total": 4,
            "users": [
                {"name": "alice", "current_connections": 37, "total_octets": 53477376},
                {"name": "sponsor_b", "current_connections": 0, "total_octets": 430080},
            ],
        },
        "system": {
            "uptime_s": 86400.5,
            "load1": 0.42,
            "mem_total_bytes": 4_000_000_000,
            "mem_available_bytes": 3_000_000_000,
            "cpu_count": 2,
        },
    }
    body.update(override)
    return body


# --- routing and the trust rule ----------------------------------------------------------------


async def test_agent_routes_live_under_agent_v1(env: Env) -> None:
    # Routed (refused for lack of the proxy secret, not "not found") under /agent/v1 only.
    for method, path in (("POST", "register"), ("POST", "renew"), ("GET", "desired"), ("POST", "report")):
        assert (await env.client.request(method, f"/agent/v1/{path}")).status_code == 403
        assert (await env.client.request(method, f"/api/agent/v1/{path}")).status_code == 404
    assert (await env.client.get("/openapi.json")).text.count("/agent/v1") == 0


async def test_proxy_secret_is_required_and_compared(env: Env) -> None:
    node_id = await create_node(env.engine)
    _, csr = make_csr(node_id)
    body = {
        "token": await env.token(node_id),
        "csr_pem": csr,
        "agent_version": "0.1.0",
        "telemt_version": None,
    }
    for headers in ({}, env.headers(secret="x" * 64), env.headers(secret=env.files.proxy_secret[:-1])):
        resp = await env.client.post("/agent/v1/register", json=body, headers=headers)
        assert resp.status_code == 403
    # The token was never touched by the refused requests.
    resp = await env.client.post("/agent/v1/register", json=body, headers=env.headers())
    assert resp.status_code == 200


async def test_forwarded_fingerprint_is_trusted_only_with_the_proxy_secret(env: Env) -> None:
    _, fp = await registered(env)
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(fp))).status_code == 200
    forged = {FP_HEADER: fp}  # a client that reaches the API without Caddy
    assert (await env.client.get("/agent/v1/desired", headers=forged)).status_code == 403
    assert (await env.client.get("/agent/v1/desired", headers=env.headers())).status_code == 401
    assert (await env.client.get("/agent/v1/desired", headers=env.headers("f" * 64))).status_code == 401
    assert (await env.client.get("/agent/v1/desired", headers=env.headers("not-hex"))).status_code == 401
    colon_upper = ":".join(fp[i : i + 2] for i in range(0, 64, 2)).upper()
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(colon_upper))).status_code == 200


async def test_missing_secret_file_fails_closed(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PROXYHUB_AGENT_PROXY_SECRET_FILE", str(tmp_path / "absent"))
    reset_config_caches()
    resp = await env.client.post("/agent/v1/register", json={}, headers=env.headers())
    assert resp.status_code == 503
    assert (await env.client.get("/agent/v1/desired", headers=env.headers("f" * 64))).status_code == 503
    weak = tmp_path / "weak"
    weak.write_text("short")
    monkeypatch.setenv("PROXYHUB_AGENT_PROXY_SECRET_FILE", str(weak))
    reset_config_caches()
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(secret="short"))).status_code == 503


# --- registration -------------------------------------------------------------------------------


async def test_register_issues_a_certificate_once_per_token(env: Env) -> None:
    node_id = await create_node(env.engine)
    token = await env.token(node_id)
    resp = await register(env, node_id, token)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()
    assert set(data) == {"cert_pem", "ca_pem"}
    assert token not in resp.text
    cert = x509.load_pem_x509_certificate(data["cert_pem"].encode())
    ca = x509.load_pem_x509_certificate(data["ca_pem"].encode())
    cert.verify_directly_issued_by(ca)
    assert data["ca_pem"] == to_pem(build_ca_certificate(load_ca_key(env.files.ca_key)))

    again = await register(env, node_id, token)
    assert again.status_code == 401 and again.json() == {"error": "invalid_token"}

    rows = await env.rows(
        "SELECT used_at IS NOT NULL, token_sha256 FROM agent_registration_tokens WHERE node_id = :n",
        n=node_id,
    )
    assert rows == [(True, token_digest(token))]
    certs = await env.rows(
        "SELECT issued_via, revoked_at FROM agent_certificates WHERE node_id = :n", n=node_id
    )
    assert certs == [("register", None)]
    agent = await env.rows(
        "SELECT agent_version, telemt_version FROM agent_nodes WHERE node_id = :n", n=node_id
    )
    assert agent == [("0.1.0", "3.5.16")]
    events = await env.rows("SELECT kind FROM node_events WHERE node_id = :n", n=node_id)
    assert ("agent_registered",) in events
    audit = await env.rows(
        "SELECT allowed FROM actions_log "
        "WHERE actor = 'agent_api' AND action = 'agent_register' AND target = :t",
        t=str(node_id),
    )
    assert audit == [(True,)]


async def test_expired_and_replaced_tokens_are_refused(env: Env) -> None:
    node_id = await create_node(env.engine)
    old = await env.token(node_id, now=datetime.now(UTC) - timedelta(hours=2))
    assert (await register(env, node_id, old)).status_code == 401  # issued 2 h ago, valid 1 h

    first = await env.token(node_id)
    second = await env.token(node_id)  # cancels the first
    assert (await register(env, node_id, first)).status_code == 401
    assert (await register(env, node_id, second)).status_code == 200


async def test_closed_nodes_get_no_token_and_cannot_register(env: Env) -> None:
    retiring = await create_node(env.engine, "retiring")
    async with env.maker()() as session:
        with pytest.raises(TokenRefused):
            await issue_registration_token(session, retiring)
        with pytest.raises(TokenRefused):
            await issue_registration_token(session, uuid.uuid4())
    node_id = await create_node(env.engine)
    token = await env.token(node_id)
    await env.rows("UPDATE nodes SET state = 'deleted' WHERE id = :n", n=node_id)
    resp = await register(env, node_id, token)
    assert resp.status_code == 403 and resp.json() == {"error": "node_closed"}


async def test_a_wrong_csr_leaves_the_token_usable(env: Env) -> None:
    node_id = await create_node(env.engine)
    token = await env.token(node_id)
    _, other_csr = make_csr(uuid.uuid4())
    bad = {"token": token, "csr_pem": other_csr, "agent_version": "0.1.0", "telemt_version": None}
    resp = await env.client.post("/agent/v1/register", json=bad, headers=env.headers())
    assert resp.status_code == 400 and resp.json() == {"error": "csr_wrong_node"}
    junk = {
        **bad,
        "csr_pem": "-----BEGIN CERTIFICATE REQUEST-----\n" + "A" * 80 + "\n-----END CERTIFICATE REQUEST-----",
    }
    resp = await env.client.post("/agent/v1/register", json=junk, headers=env.headers())
    assert resp.status_code == 400 and resp.json() == {"error": "csr_unparseable"}
    assert (await register(env, node_id, token)).status_code == 200
    refused = await env.rows(
        "SELECT reasons->>'code' FROM actions_log "
        "WHERE actor = 'agent_api' AND allowed = false AND target = :t",
        t=str(node_id),
    )
    assert sorted(r[0] for r in refused) == ["csr_unparseable", "csr_wrong_node"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: {**b, "extra": 1},
        lambda b: {k: v for k, v in b.items() if k != "agent_version"},
        lambda b: {**b, "token": 123},
        lambda b: {**b, "agent_version": "1.2.3.4"},
        lambda b: {**b, "agent_version": "a b"},
        lambda b: [b],
    ],
)
async def test_register_body_is_strict_and_never_echoed(env: Env, mutate: Any) -> None:
    node_id = await create_node(env.engine)
    token = await env.token(node_id)
    _, csr = make_csr(node_id)
    body = mutate({"token": token, "csr_pem": csr, "agent_version": "0.1.0", "telemt_version": None})
    resp = await env.client.post("/agent/v1/register", json=body, headers=env.headers())
    assert resp.status_code == 422
    assert token not in resp.text and "BEGIN" not in resp.text
    assert (await register(env, node_id, token)).status_code == 200  # token untouched


async def test_reregistration_revokes_the_previous_identity(env: Env) -> None:
    node_id, first_fp = await registered(env)
    resp = await register(env, node_id)
    assert resp.status_code == 200
    second_fp = fingerprint(resp.json()["cert_pem"])
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(first_fp))).status_code == 401
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(second_fp))).status_code == 200
    reasons = await env.rows(
        "SELECT revoked_reason FROM agent_certificates WHERE node_id = :n ORDER BY id", n=node_id
    )
    assert reasons == [("re_registered",), (None,)]


# --- identity -----------------------------------------------------------------------------------


async def test_only_valid_unrevoked_certificates_of_live_nodes_authenticate(env: Env) -> None:
    node_id, fp = await registered(env)

    async def status() -> int:
        return (await env.client.get("/agent/v1/desired", headers=env.headers(fp))).status_code

    assert await status() == 200
    await env.rows(
        "UPDATE agent_certificates SET not_after = now() - interval '1 second' WHERE node_id = :n", n=node_id
    )
    assert await status() == 401
    await env.rows(
        "UPDATE agent_certificates SET not_after = now() + interval '1 day', "
        "not_before = now() + interval '1 hour' WHERE node_id = :n",
        n=node_id,
    )
    assert await status() == 401
    await env.rows(
        "UPDATE agent_certificates SET not_before = now() - interval '1 hour' WHERE node_id = :n", n=node_id
    )
    assert await status() == 200
    await env.rows(
        "UPDATE agent_certificates SET revoked_at = now(), revoked_reason = 'manual' WHERE node_id = :n",
        n=node_id,
    )
    assert await status() == 401
    await env.rows(
        "UPDATE agent_certificates SET revoked_at = NULL, revoked_reason = NULL WHERE node_id = :n", n=node_id
    )
    await env.rows("UPDATE nodes SET state = 'deleted' WHERE id = :n", n=node_id)
    assert await status() == 401


# --- desired state ------------------------------------------------------------------------------


async def test_desired_state_with_etag(env: Env) -> None:
    node_id, fp = await registered(env)
    empty = await env.client.get("/agent/v1/desired", headers=env.headers(fp))
    assert empty.status_code == 200 and empty.json()["telemt"] is None

    expires = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    async with env.maker()() as session:
        await set_telemt_node_config(
            session, node_id, ports=[443, 8443], tls_domain="cdn.example.com", ad_tag=TAG_A
        )
        await create_telemt_user(
            session, env.seal_key, node_id, "alice", expires_at=expires, max_tcp_conns=500
        )
        await create_telemt_user(
            session, env.seal_key, node_id, "sponsor_b", ad_tag=TAG_B, data_quota_bytes=10**11
        )
        await session.commit()
        alice_secret = await reveal_secret(session, env.seal_key, node_id, "alice")

    resp = await env.client.get("/agent/v1/desired", headers=env.headers(fp))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    etag = resp.headers["etag"]
    assert etag == '"' + hashlib.sha256(resp.content).hexdigest() + '"'
    doc = resp.json()
    assert doc["format"] == 1 and doc["node_id"] == str(node_id)
    t = doc["telemt"]
    assert t["use_middle_proxy"] is True and t["beobachten"] is False and t["log_level"] == "silent"
    assert t["ports"] == [443, 8443] and t["tls_domain"] == "cdn.example.com" and t["ad_tag"] == TAG_A
    assert t["modes"] == {"classic": False, "secure": False, "tls": True}
    users = {u["name"]: u for u in t["users"]}
    assert users["alice"]["secret"] == alice_secret and len(alice_secret) == 32
    assert users["alice"]["expires_at"] == "2026-12-31T23:59:59Z" and users["alice"]["max_tcp_conns"] == 500
    assert users["sponsor_b"]["ad_tag"] == TAG_B and users["sponsor_b"]["data_quota_bytes"] == 10**11
    assert users["alice"]["secret"] != users["sponsor_b"]["secret"]
    served = await env.rows("SELECT last_served_etag FROM agent_nodes WHERE node_id = :n", n=node_id)
    assert served == [(etag,)]

    not_modified = await env.client.get(
        "/agent/v1/desired", headers={**env.headers(fp), "If-None-Match": etag}
    )
    assert not_modified.status_code == 304 and not_modified.content == b""
    assert not_modified.headers["etag"] == etag

    async with env.maker()() as session:
        await set_user_sponsor_tag(session, node_id, "alice", TAG_B)
        await session.commit()
    changed = await env.client.get("/agent/v1/desired", headers={**env.headers(fp), "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert {u["name"]: u for u in changed.json()["telemt"]["users"]}["alice"]["ad_tag"] == TAG_B


async def test_desired_state_rules(env: Env) -> None:
    node_id = await create_node(env.engine)
    async with env.maker()() as session:
        for kwargs in (
            {"ports": [], "tls_domain": "a.example.com"},
            {"ports": [443, 443], "tls_domain": "a.example.com"},
            {"ports": [9090], "tls_domain": "a.example.com"},
            {"ports": [70000], "tls_domain": "a.example.com"},
            {"ports": [443], "tls_domain": "10.1.2.3"},
            {"ports": [443], "tls_domain": "Example.com"},
            {"ports": [443], "tls_domain": "a.example.com", "ad_tag": "XYZ"},
            {"ports": [443], "tls_domain": "a.example.com", "tls": False},
        ):
            with pytest.raises(DesiredStateError):
                await set_telemt_node_config(session, node_id, **kwargs)  # type: ignore[arg-type]
        for name in ("include", "agent_placeholder", "Alice", "1abc", "a" * 33, "a-b"):
            with pytest.raises(DesiredStateError):
                await create_telemt_user(session, env.seal_key, node_id, name)
        with pytest.raises(DesiredStateError):
            naive = datetime(2027, 1, 1).replace(tzinfo=None)
            await create_telemt_user(session, env.seal_key, node_id, "naive", expires_at=naive)


# --- renewal ------------------------------------------------------------------------------------


async def test_renewal_at_two_thirds_and_supersession(env: Env) -> None:
    node_id, old_fp = await registered(env)
    _, csr = make_csr(node_id)
    early = await env.client.post("/agent/v1/renew", json={"csr_pem": csr}, headers=env.headers(old_fp))
    assert early.status_code == 409 and early.json() == {"error": "renewal_too_early"}

    # 25 of 30 days used: past the two-thirds point.
    await env.rows(
        "UPDATE agent_certificates SET not_before = now() - interval '25 days', "
        "not_after = now() + interval '5 days' WHERE node_id = :n",
        n=node_id,
    )
    _, wrong = make_csr(uuid.uuid4())
    refused = await env.client.post("/agent/v1/renew", json={"csr_pem": wrong}, headers=env.headers(old_fp))
    assert refused.status_code == 400 and refused.json() == {"error": "csr_wrong_node"}
    resp = await env.client.post("/agent/v1/renew", json={"csr_pem": csr}, headers=env.headers(old_fp))
    assert resp.status_code == 200, resp.text
    new_fp = fingerprint(resp.json()["cert_pem"])
    # Until the new certificate is used, the old one still works (a lost response never locks out).
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(old_fp))).status_code == 200
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(new_fp))).status_code == 200
    assert (await env.client.get("/agent/v1/desired", headers=env.headers(old_fp))).status_code == 401
    rows = await env.rows(
        "SELECT issued_via, revoked_reason FROM agent_certificates WHERE node_id = :n ORDER BY id", n=node_id
    )
    assert rows == [("register", "superseded"), ("renew", None)]
    assert ("agent_cert_renewed",) in await env.rows(
        "SELECT kind FROM node_events WHERE node_id = :n", n=node_id
    )


# --- reports ------------------------------------------------------------------------------------


async def test_report_is_stored_as_aggregates(env: Env) -> None:
    node_id, fp = await registered(env)
    reg_id = await create_registration(env.engine, "alice")
    async with env.maker()() as session:
        await set_telemt_node_config(session, node_id, ports=[443], tls_domain="cdn.example.com")
        await create_telemt_user(session, env.seal_key, node_id, "alice", registration_id=reg_id)
        await create_telemt_user(session, env.seal_key, node_id, "sponsor_b")
        await session.commit()
    body = report_body()
    body["telemt"]["users"].append({"name": "gone_user", "current_connections": 1, "total_octets": 2})
    resp = await env.client.post("/agent/v1/report", json=body, headers=env.headers(fp))
    assert resp.status_code == 204, resp.text

    stats = await env.rows(
        "SELECT source, current_connections, total_octets, extra->>'node_id' FROM mtproxy_stats "
        "WHERE registration_id = :r",
        r=reg_id,
    )
    assert stats == [("node", 37, 53477376, str(node_id))]
    users = await env.rows(
        "SELECT name, last_current_connections, last_total_octets FROM telemt_users "
        "WHERE node_id = :n ORDER BY name",
        n=node_id,
    )
    assert users == [("alice", 37, 53477376), ("sponsor_b", 0, 430080)]
    reports = await env.rows(
        "SELECT telemt_up, connections_total, users_reported, cpu_count, apply_ok FROM agent_node_reports "
        "WHERE node_id = :n",
        n=node_id,
    )
    assert reports == [(True, 1532, 3, 2, True)]
    agent = await env.rows(
        "SELECT last_report_at IS NOT NULL, last_applied_etag, telemt_version "
        "FROM agent_nodes WHERE node_id = :n",
        n=node_id,
    )
    assert agent == [(True, '"' + "c" * 64 + '"', "3.5.16")]


def _with(path: list[str | int], value: Any) -> dict[str, Any]:
    body = report_body()
    target: Any = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return body


IP_LIKE = [
    _with(["agent_version"], "10.0.0.7"),
    _with(["telemt", "version"], "192.168.1.20"),
    _with(["telemt", "users", 0, "name"], "2001:db8::1"),
    _with(["apply_error"], "conn_from_203.0.113.9"),
    _with(
        ["telemt", "users"], [{"name": "alice", "current_connections": 1, "total_octets": 1, "peer": "::1"}]
    ),
    {**report_body(), "203.0.113.9": 1},
]
SCHEMA = [
    {**report_body(), "extra": True},
    _with(["telemt", "users", 0, "ips"], []),
    _with(["telemt", "users", 0, "current_connections"], "37"),
    _with(["telemt", "users", 0, "current_connections"], 37.5),
    _with(["telemt", "users", 0, "total_octets"], -1),
    _with(["telemt", "up"], 1),
    _with(["format"], 2),
    _with(["applied_etag"], "W/abc"),
    _with(["telemt", "users"], [{"name": "alice", "current_connections": 1, "total_octets": 1}] * 2),
    _with(["telemt", "users"], [{"name": "include", "current_connections": 1, "total_octets": 1}]),
    _with(
        ["telemt", "users"],
        [{"name": f"u{i}", "current_connections": 0, "total_octets": 0} for i in range(1101)],
    ),
    {k: v for k, v in report_body().items() if k != "system"},
]


@pytest.mark.parametrize("body", IP_LIKE + SCHEMA)
async def test_report_rejections_store_nothing(env: Env, body: dict[str, Any]) -> None:
    node_id, fp = await registered(env)
    resp = await env.client.post("/agent/v1/report", json=body, headers=env.headers(fp))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] in {"ip_like_value", "schema"}
    assert await env.rows("SELECT 1 FROM agent_node_reports WHERE node_id = :n", n=node_id) == []


async def test_ip_like_values_get_their_own_code(env: Env) -> None:
    _, fp = await registered(env)
    for body in IP_LIKE:
        resp = await env.client.post("/agent/v1/report", json=body, headers=env.headers(fp))
        assert resp.json() == {"error": "ip_like_value"}


async def test_report_needs_identity_and_size_limit(env: Env) -> None:
    _, fp = await registered(env)
    assert (
        await env.client.post("/agent/v1/report", json=report_body(), headers=env.headers())
    ).status_code == 401
    big = b'{"format": 1, "pad": "' + b"x" * (600 * 1024) + b'"}'
    resp = await env.client.post(
        "/agent/v1/report", content=big, headers={**env.headers(fp), "Content-Type": "application/json"}
    )
    assert resp.status_code == 413
    bad_json = await env.client.post("/agent/v1/report", content=b"{", headers=env.headers(fp))
    assert bad_json.status_code == 400


# --- secrets ------------------------------------------------------------------------------------


async def test_no_token_or_secret_in_logs_responses_or_database(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    node_id = await create_node(env.engine)
    token = await env.token(node_id)
    async with env.maker()() as session:
        await set_telemt_node_config(session, node_id, ports=[443], tls_domain="cdn.example.com")
        await create_telemt_user(session, env.seal_key, node_id, "alice")
        await session.commit()
        secret = await reveal_secret(session, env.seal_key, node_id, "alice")
    reg = await register(env, node_id, token)
    fp = fingerprint(reg.json()["cert_pem"])
    await register(env, node_id, token)  # refused reuse
    await env.client.get("/agent/v1/desired", headers=env.headers(fp))
    await env.client.post("/agent/v1/report", json=report_body(), headers=env.headers(fp))
    await env.client.post(
        "/agent/v1/report", json=_with(["agent_version"], "10.0.0.7"), headers=env.headers(fp)
    )

    logs = caplog.text
    for value in (token, secret, env.files.proxy_secret):
        assert value not in logs
    dump = []
    async with env.engine.connect() as conn:
        for table in (
            "actions_log",
            "node_events",
            *("agent_registration_tokens", "agent_nodes", "telemt_users"),
        ):
            result = await conn.execute(text(f"SELECT row_to_json(t)::text FROM {table} t"))  # noqa: S608
            dump.extend(result.scalars().all())
    text_dump = "\n".join(dump)
    assert token not in text_dump and secret not in text_dump and env.files.proxy_secret not in text_dump
    sealed = await env.rows(
        "SELECT octet_length(secret_sealed) FROM telemt_users WHERE node_id = :n", n=node_id
    )
    assert sealed == [(45,)]


def test_sealed_secrets_are_bound_to_their_row(tmp_path: Path) -> None:
    key = load_seal_key(write_secret_files(tmp_path).master_key)
    node_id = uuid.uuid4()
    raw = bytes(range(16))
    blob = seal_secret(key, node_id, "alice", raw)
    assert len(blob) == 45 and blob != seal_secret(key, node_id, "alice", raw)  # fresh nonce
    assert open_secret(key, node_id, "alice", blob) == raw
    for other_node, other_name in ((uuid.uuid4(), "alice"), (node_id, "bob")):
        with pytest.raises(SealError):
            open_secret(key, other_node, other_name, blob)
    other_key = load_seal_key(write_secret_files(tmp_path / "other").master_key)
    with pytest.raises(SealError):
        open_secret(other_key, node_id, "alice", blob)
    tampered = bytearray(blob)
    tampered[20] ^= 1
    with pytest.raises(SealError):
        open_secret(key, node_id, "alice", bytes(tampered))


@pytest.mark.parametrize(
    ("value", "ip"),
    [
        ("10.0.0.1", True),
        ("::1", True),
        ("[2001:db8::1]", True),
        ("fe80::1%eth0", True),
        ("::ffff:192.0.2.1", True),
        ("x192.168.0.1y", True),
        ("2001:db8:0:0:0:0:0:1", True),
        ("3.5.16", False),
        ("0.1.0", False),
        ("alice", False),
        ('"' + "a" * 64 + '"', False),
        ("render_refused", False),
        ("1.2.3.4.5", True),  # contains an IPv4 shape: refused too
    ],
)
def test_ip_detection(value: str, ip: bool) -> None:
    assert looks_like_ip(value) is ip


def test_domain_rule() -> None:
    assert valid_domain("cdn.example.com")
    assert not valid_domain("10.1.2.3")
    assert not valid_domain("localhost")
    assert not valid_domain("Example.com")
    assert not valid_domain("a" * 64 + ".com")


async def test_migration_matches_models(env: Env) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import proxyhub.db.models_agent  # noqa: F401 - registers the agent tables on Base.metadata
    from proxyhub.db.models import Base

    mine = {
        "agent_ca_certificates",
        "agent_registration_tokens",
        "agent_certificates",
        "agent_nodes",
        "telemt_node_configs",
        "telemt_users",
        "agent_node_reports",
    }

    def diff(sync_conn: Any) -> list[Any]:
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with env.engine.connect() as conn:
        changes = await conn.run_sync(diff)

    def table_of(change: Any) -> str | None:
        item = change[0] if isinstance(change, list) else change
        for part in item[1:]:
            name = getattr(part, "name", None) or getattr(getattr(part, "table", None), "name", None)
            if isinstance(part, str):
                name = part
            if name in mine:
                return name
        return None

    ours = [c for c in changes if table_of(c) is not None]
    assert ours == [], json.dumps([repr(c) for c in ours])
